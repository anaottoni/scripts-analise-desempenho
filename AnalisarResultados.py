import argparse
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd


PASTA_EXPERIMENTO = Path(__file__).resolve().parent
PASTA_RESULTADOS = PASTA_EXPERIMENTO / "resultados_comparacao"
LABEL_FLUXO = "Fluxo completo"
FATORES_MEMORIA = {
    "B": 1,
    "kB": 1000,
    "KB": 1000,
    "KiB": 1024,
    "MB": 1000**2,
    "MiB": 1024**2,
    "GB": 1000**3,
    "GiB": 1024**3,
    "TB": 1000**4,
    "TiB": 1024**4,
}


def coluna(tabela: pd.DataFrame, nome: str) -> str:
    por_nome = {
        str(c).lstrip("\ufeff").strip().lower(): str(c) for c in tabela.columns
    }
    if nome.lower() not in por_nome:
        raise ValueError(
            f"Coluna {nome!r} não encontrada. Colunas no arquivo: {list(tabela.columns)}"
        )
    return por_nome[nome.lower()]


def cpu_percentual(valor: object) -> float:
    texto = str(valor).strip().replace("%", "").replace(",", ".")
    if not texto:
        raise ValueError("O CSV de docker stats contém percentual de CPU vazio.")
    return float(texto)


def memoria_bytes(valor: object) -> int:
    usada = str(valor).split("/", maxsplit=1)[0].strip().replace(",", ".")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([A-Za-z]+)", usada)
    if match is None:
        raise ValueError(f"Valor de memória docker stats inválido: {valor!r}")
    quantidade, unidade = float(match.group(1)), match.group(2)
    if unidade not in FATORES_MEMORIA:
        raise ValueError(f"Unidade de memória não reconhecida: {unidade}")
    return int(quantidade * FATORES_MEMORIA[unidade])


def analisar_jmeter(arquivo: Path, arquitetura: str, bateria: int) -> dict[str, object]:
    dados = pd.read_csv(arquivo)
    label_col = coluna(dados, "label")
    success_col = coluna(dados, "success")
    labels = dados[label_col].fillna("").astype(str)
    sucesso = dados[success_col].astype(str).str.lower().isin(("true", "1"))

    fluxo = labels.str.contains(LABEL_FLUXO, case=False, regex=False)
    if not fluxo.any():
        raise ValueError(
            f"{arquivo} não contém transações '{LABEL_FLUXO}'. "
            "Confira se o Transaction Controller está ativo no plano JMeter."
        )

    fluxos_totais = int(fluxo.sum())
    fluxos_ok = int((fluxo & sucesso).sum())
    fluxos_falha = fluxos_totais - fluxos_ok
    timestamp_col = coluna(dados, "timeStamp")
    elapsed_col = coluna(dados, "elapsed")
    timestamps = pd.to_numeric(dados.loc[fluxo, timestamp_col], errors="coerce")
    elapsed_ms = pd.to_numeric(dados.loc[fluxo, elapsed_col], errors="coerce")
    if timestamps.isna().any() or elapsed_ms.isna().any():
        raise ValueError(
            f"{arquivo} contém timestamps ou durações inválidos nos fluxos JMeter."
        )
    duracao_medicao = (
        timestamps.add(elapsed_ms).max() - timestamps.min()
    ) / 1000
    if duracao_medicao <= 0:
        raise ValueError(
            f"{arquivo} não permite determinar a duração da medição a partir "
            "dos timestamps dos fluxos."
        )
    response_code_col = coluna(dados, "responseCode")
    codigos_falha = (
        dados.loc[fluxo & ~sucesso, response_code_col]
        .fillna("desconhecido")
        .astype(str)
        .value_counts()
        .sort_index()
    )
    return {
        "arquitetura": arquitetura,
        "bateria": bateria,
        "fluxos_totais": fluxos_totais,
        "fluxos_sucesso": fluxos_ok,
        "fluxos_falha": fluxos_falha,
        "duracao_medicao_segundos": float(duracao_medicao),
        "taxa_falha_fluxos_percentual": 100 * fluxos_falha / fluxos_totais,
        "codigos_http_falha": "; ".join(
            f"{codigo}:{quantidade}" for codigo, quantidade in codigos_falha.items()
        ),
        "bateria_valida": fluxos_totais > 0 and fluxos_falha == 0,
        "throughput_fluxos_rps": fluxos_ok / duracao_medicao,
    }


def analisar_docker_stats(
    arquivo: Path, arquitetura: str, bateria: int
) -> tuple[dict[str, object], pd.DataFrame]:
    dados = pd.read_csv(arquivo)
    timestamp_col = coluna(dados, "timestamp")
    container_col = coluna(dados, "container")
    cpu_col = coluna(dados, "cpu_percent")
    memoria_col = coluna(dados, "memory_usage")

    dados["timestamp"] = pd.to_datetime(dados[timestamp_col], errors="coerce", utc=True)
    if dados["timestamp"].isna().any():
        raise ValueError(f"{arquivo} contém timestamps inválidos.")
    dados["container"] = dados[container_col].astype(str)
    dados["cpu_percent"] = dados[cpu_col].map(cpu_percentual)
    dados["memoria_bytes"] = dados[memoria_col].map(memoria_bytes)

    serie = (
        dados.groupby("timestamp", as_index=False)
        .agg(
            cpu_aplicacao_percent=("cpu_percent", "sum"),
            memoria_aplicacao_bytes=("memoria_bytes", "sum"),
            containers=("container", "nunique"),
        )
        .sort_values("timestamp")
    )
    if serie.empty:
        raise ValueError(f"{arquivo} não contém amostras Docker.")
    serie["memoria_aplicacao_mib"] = serie["memoria_aplicacao_bytes"] / (1024**2)
    serie["arquitetura"] = arquitetura
    serie["bateria"] = bateria

    resumo = {
        "cpu_media_percent": float(serie["cpu_aplicacao_percent"].mean()),
        "cpu_pico_percent": float(serie["cpu_aplicacao_percent"].max()),
        "ram_media_mib": float(serie["memoria_aplicacao_mib"].mean()),
        "ram_pico_mib": float(serie["memoria_aplicacao_mib"].max()),
    }
    return resumo, serie[
        [
            "arquitetura",
            "bateria",
            "timestamp",
            "containers",
            "cpu_aplicacao_percent",
            "memoria_aplicacao_mib",
        ]
    ]


def salvar_grafico(
    dados: pd.DataFrame,
    metrica: str,
    titulo: str,
    eixo_y: str,
    destino: Path,
) -> None:
    arquiteturas = ["monolito", "microsservicos"]
    valores = [
        dados.loc[dados["arquitetura"] == nome, metrica].dropna().tolist()
        for nome in arquiteturas
    ]
    if any(not amostras for amostras in valores):
        raise ValueError(f"Faltam valores de {metrica} para gerar o gráfico comparativo.")

    figura, eixo = plt.subplots(figsize=(8, 5))
    eixo.boxplot(valores, tick_labels=arquiteturas, showmeans=True)
    eixo.set_title(titulo)
    eixo.set_ylabel(eixo_y)
    eixo.grid(axis="y", alpha=0.3)
    figura.tight_layout()
    figura.savefig(destino, dpi=160)
    plt.close(figura)


def salvar_grafico_falhas(dados: pd.DataFrame, destino: Path) -> None:
    valores = [
        dados.loc[dados["arquitetura"] == arquitetura, "taxa_falha_fluxos_percentual"]
        .dropna()
        .tolist()
        for arquitetura in ("monolito", "microsservicos")
    ]
    if any(not amostras for amostras in valores):
        return

    figura, eixo = plt.subplots(figsize=(8, 5))
    eixo.boxplot(valores, tick_labels=["monolito", "microsservicos"], showmeans=True)
    eixo.set_title("Taxa de falha dos fluxos (diagnóstico)")
    eixo.set_ylabel("Fluxos com falha (%)")
    eixo.set_ylim(0, 100)
    eixo.grid(axis="y", alpha=0.3)
    figura.tight_layout()
    figura.savefig(destino, dpi=160)
    plt.close(figura)


def analisar(pasta_resultados: Path, pasta_saida: Path) -> None:
    resumos: list[dict[str, object]] = []
    series: list[pd.DataFrame] = []

    for arquitetura in ("monolito", "microsservicos"):
        pasta = pasta_resultados / arquitetura
        arquivos_jmeter = sorted(pasta.glob("bateria_*_jmeter.csv"))
        if not arquivos_jmeter:
            raise FileNotFoundError(f"Não encontrei JMeter CSV em {pasta}.")

        for arquivo_jmeter in arquivos_jmeter:
            match = re.fullmatch(r"bateria_(\d+)_jmeter\.csv", arquivo_jmeter.name)
            if match is None:
                continue
            bateria = int(match.group(1))
            arquivo_stats = pasta / f"bateria_{bateria:02d}_docker_stats.csv"
            if not arquivo_stats.is_file():
                raise FileNotFoundError(f"CSV docker stats ausente: {arquivo_stats}")

            resumo = analisar_jmeter(arquivo_jmeter, arquitetura, bateria)
            resumo_stats, serie = analisar_docker_stats(
                arquivo_stats, arquitetura, bateria
            )
            resumo.update(resumo_stats)
            resumos.append(resumo)
            series.append(serie)

    tabela = pd.DataFrame(resumos).sort_values(["arquitetura", "bateria"])
    validade_por_bateria = tabela.pivot(
        index="bateria", columns="arquitetura", values="bateria_valida"
    ).fillna(False)
    baterias_comparaveis = (
        validade_por_bateria.index[
            validade_por_bateria.reindex(
                columns=["monolito", "microsservicos"], fill_value=False
            ).all(axis=1)
        ].tolist()
        if {"monolito", "microsservicos"}.issubset(validade_por_bateria.columns)
        else []
    )
    tabela["comparacao_valida"] = (
        tabela["bateria"].isin(baterias_comparaveis) & tabela["bateria_valida"]
    )
    comparacoes_validas = tabela.loc[tabela["comparacao_valida"]]
    if comparacoes_validas.empty:
        resumo_arquiteturas = pd.DataFrame(
            columns=[
                "arquitetura",
                "baterias",
                "throughput_fluxos_rps_media",
                "cpu_media_percent",
                "cpu_pico_percent",
                "ram_media_mib",
                "ram_pico_mib",
            ]
        )
    else:
        resumo_arquiteturas = (
            comparacoes_validas.groupby("arquitetura", as_index=False)
            .agg(
                baterias=("bateria", "nunique"),
                throughput_fluxos_rps_media=("throughput_fluxos_rps", "mean"),
                cpu_media_percent=("cpu_media_percent", "mean"),
                cpu_pico_percent=("cpu_pico_percent", "mean"),
                ram_media_mib=("ram_media_mib", "mean"),
                ram_pico_mib=("ram_pico_mib", "mean"),
            )
        )

    pasta_saida.mkdir(parents=True, exist_ok=True)
    tabela.to_csv(pasta_saida / "resumo_por_bateria.csv", index=False)
    resumo_arquiteturas.to_csv(
        pasta_saida / "resumo_por_arquitetura.csv", index=False
    )
    pd.concat(series, ignore_index=True).to_csv(
        pasta_saida / "serie_docker_stats.csv", index=False
    )
    salvar_grafico_falhas(tabela, pasta_saida / "taxa_falhas_fluxo.png")


    if not comparacoes_validas.empty:
        colunas_interesse = [
            "bateria",
            "arquitetura",
            "throughput_fluxos_rps",
            "cpu_media_percent",
            "ram_media_mib",
        ]

        df_jasp = comparacoes_validas[colunas_interesse].copy()
        
        df_jasp.to_csv(pasta_saida / "dados_jasp.csv", index=False)

    metricas_comparativas = (
        ("throughput_fluxos_rps", "Throughput de fluxos completos bem-sucedidos",
         "Fluxos por segundo (RPS)", "throughput.png"),
        ("cpu_media_percent", "CPU média agregada dos containers da aplicação",
         "CPU (%)", "cpu.png"),
        ("ram_media_mib", "RAM média agregada dos containers da aplicação",
         "RAM (MiB)", "ram.png"),
    )
    if comparacoes_validas.empty:
        for _, _, _, nome_arquivo in metricas_comparativas:
            arquivo_antigo = pasta_saida / nome_arquivo
            if arquivo_antigo.is_file():
                arquivo_antigo.unlink()
        print(
            "Nenhuma bateria pareada sem falhas nos fluxos; comparação de desempenho "
            "não gerada. Consulte resumo_por_bateria.csv e taxa_falhas_fluxo.png."
        )
    else:
        for metrica, titulo, eixo_y, nome_arquivo in metricas_comparativas:
            salvar_grafico(
                comparacoes_validas,
                metrica,
                titulo,
                eixo_y,
                pasta_saida / nome_arquivo,
            )

    print(f"Análise concluída. CSVs e gráficos: {pasta_saida}")
    if not resumo_arquiteturas.empty:
        print(resumo_arquiteturas.to_string(index=False))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analisa throughput do JMeter e CPU/RAM de docker stats."
    )
    parser.add_argument(
        "--resultados",
        type=Path,
        default=PASTA_RESULTADOS,
        help="Pasta contendo os diretórios monolito e microsservicos.",
    )
    parser.add_argument(
        "--saida",
        type=Path,
        default=None,
        help="Pasta para os resumos e gráficos (padrão: <resultados>/analise).",
    )
    args = parser.parse_args()
    analisar(args.resultados, args.saida or args.resultados / "analise")


if __name__ == "__main__":
    main()