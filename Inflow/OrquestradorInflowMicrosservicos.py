import csv
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


NUM_RODADAS = 30
TEMPO_AQUECIMENTO = 60
TEMPO_MEDICAO = 120
TEMPO_MAXIMO_INICIALIZACAO = 300
INTERVALO_MONITORAMENTO = 1
JMETER_HOME: Path | None = Path(r"C:\Users\amand\Downloads\apache-jmeter-5.6.3\apache-jmeter-5.6.3")

PASTA_EXPERIMENTO = Path(__file__).resolve().parent

PASTA_PROJETO = PASTA_EXPERIMENTO / "Inflow-micro"
ARQUIVO_COMPOSE = PASTA_PROJETO / "docker-compose.benchmark.yml"
PLANO_JMETER = PASTA_EXPERIMENTO / "jmeter" / "InflowMicrosservicos.jmx"
PASTA_RESULTADOS = PASTA_EXPERIMENTO / "resultados_comparacao" / "inflow_microsservicos"
URL_BASE = "http://localhost:5000"  # API Gateway
URLS_PRONTIDAO = (
    f"{URL_BASE}/",
    f"{URL_BASE}/users-service",
    f"{URL_BASE}/customers-service",
    f"{URL_BASE}/payments-service",
    f"{URL_BASE}/wallets-service",
)

SERVICOS_MONITORADOS: tuple[str, ...] = ()
SERVICOS_COM_DOCKERFILE = ("APIGateway", "Customers", "Payments", "Users", "Wallets", "Saga")


def localizar_jmeter() -> str:
    if JMETER_HOME is not None:
        pasta_bin = JMETER_HOME / "bin"
        for nome in ("jmeter.bat", "jmeter.cmd", "jmeter"):
            executavel = pasta_bin / nome
            if executavel.is_file():
                return str(executavel)
        raise FileNotFoundError(
            f"JMeter não encontrado em {pasta_bin}. Ajuste JMETER_HOME neste arquivo."
        )

    for nome in ("jmeter", "jmeter.bat", "jmeter.cmd"):
        executavel = shutil.which(nome)
        if executavel:
            return executavel
    raise FileNotFoundError("JMeter não encontrado no PATH. Configure JMETER_HOME.")


def limpar_arquivos(*caminhos: Path) -> None:
    """O JMeter faz append no CSV se o arquivo já existir; isso polui as métricas."""
    for caminho in caminhos:
        if caminho.is_file():
            caminho.unlink()


def montar_comando_shell(comando: list[str], *, call: bool = False) -> str:
    """Monta linha de comando segura no Windows (aspas em -D e caminhos com espaço)."""
    executavel = comando[0]
    prefixo = f'call "{executavel}"' if call else f'"{executavel}"'
    partes = [prefixo]
    for arg in comando[1:]:
        if arg.startswith("-D") or " " in arg:
            partes.append(f'"{arg}"')
        else:
            partes.append(arg)
    return " ".join(partes)


def executar_comando(comando: list[str], *, call: bool = False) -> None:
    if os.name == "nt" and (
        call or Path(comando[0]).suffix.lower() in {".bat", ".cmd"}
    ):
        subprocess.run(
            montar_comando_shell(comando, call=call or True),
            cwd=PASTA_PROJETO,
            check=True,
            shell=True,
        )
    else:
        subprocess.run(comando, cwd=PASTA_PROJETO, check=True)


def limpar_cache_buildkit() -> None:
    """Evita falha intermitente de snapshot corrompido no Docker BuildKit."""
    subprocess.run(
        ["docker", "builder", "prune", "-f"],
        cwd=PASTA_PROJETO,
        check=False,
        capture_output=True,
        text=True,
    )


def corrigir_dockerfiles() -> None:
    """Os .csproj do Inflow-micro são net6.0, mas os Dockerfiles usam SDK/runtime 5.0 (build falha).
    Alinha para 6.0, a mesma versão do monólito, para a comparação ser justa. Idempotente."""
    for pasta in SERVICOS_COM_DOCKERFILE:
        dockerfile = PASTA_PROJETO / "src" / pasta / "Dockerfile"
        if not dockerfile.is_file():
            raise FileNotFoundError(f"Dockerfile não encontrado: {dockerfile}")
        texto = dockerfile.read_text(encoding="utf-8")
        novo = texto.replace("dotnet/sdk:5.0", "dotnet/sdk:6.0").replace(
            "dotnet/aspnet:5.0", "dotnet/aspnet:6.0"
        )
        if novo != texto:
            dockerfile.write_text(novo, encoding="utf-8")
            print(f"Dockerfile ajustado para .NET 6.0: {dockerfile}")


def executar_jmeter(comando: list[str]) -> None:
    executar_comando(comando, call=True)


def _verificar(url: str) -> tuple[bool, str]:
    """Retorna (de_pe, detalhe). Resposta < 500 = de pé (ex.: 401/404)."""
    try:
        with urlopen(url, timeout=3) as resposta:
            return True, f"HTTP {resposta.status}"
    except HTTPError as erro:
        return erro.code < 500, f"HTTP {erro.code}"
    except (OSError, URLError, TimeoutError) as erro:
        return False, f"{type(erro).__name__}: {erro}"


def aguardar_aplicacao() -> None:
    limite = time.monotonic() + TEMPO_MAXIMO_INICIALIZACAO
    ultimo: dict[str, str] = {}
    while time.monotonic() < limite:
        resultados = {url: _verificar(url) for url in URLS_PRONTIDAO}
        ultimo = {url: detalhe for url, (_, detalhe) in resultados.items()}
        if all(ok for ok, _ in resultados.values()):
            # Pequena estabilização pós-JIT antes do aquecimento JMeter.
            time.sleep(5)
            return
        time.sleep(2)
    resumo = "; ".join(f"{url} -> {detalhe}" for url, detalhe in ultimo.items())
    raise TimeoutError(f"Microsserviços Inflow indisponíveis em {URL_BASE}. Último estado: {resumo}")


def obter_ids_containers(compose: list[str]) -> list[str]:
    resultado = subprocess.run(
        [*compose, "ps", "-q", *SERVICOS_MONITORADOS],
        cwd=PASTA_PROJETO,
        check=True,
        capture_output=True,
        text=True,
    )
    ids = resultado.stdout.split()
    if not ids:
        raise RuntimeError("Nenhum container dos microsserviços foi encontrado para monitorar.")
    return ids


def monitorar_docker(
    ids_containers: list[str],
    arquivo_saida: Path,
    parar: threading.Event,
    erros: list[str],
) -> None:
    comando = [
        "docker",
        "stats",
        "--no-stream",
        "--format",
        "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}",
        *ids_containers,
    ]
    try:
        with arquivo_saida.open("w", newline="", encoding="utf-8") as arquivo:
            escritor = csv.writer(arquivo)
            escritor.writerow(
                ["timestamp", "container", "cpu_percent", "memory_usage"]
            )
            while not parar.is_set():
                resultado = subprocess.run(
                    comando,
                    cwd=PASTA_PROJETO,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
                for linha in resultado.stdout.splitlines():
                    dados = linha.split("\t")
                    if len(dados) == 3:
                        escritor.writerow([timestamp, *dados])
                arquivo.flush()
                parar.wait(INTERVALO_MONITORAMENTO)
    except (OSError, subprocess.SubprocessError) as erro:
        erros.append(str(erro))


def validar_aquecimento(arquivo: Path) -> None:
    with arquivo.open(newline="", encoding="utf-8-sig") as entrada:
        leitor = csv.DictReader(entrada)
        if not leitor.fieldnames:
            raise RuntimeError(f"CSV de aquecimento vazio ou inválido: {arquivo}")
        colunas = {nome.strip().lower(): nome for nome in leitor.fieldnames}
        if not {"label", "success"}.issubset(colunas):
            raise RuntimeError(
                f"CSV de aquecimento sem colunas label/success: {arquivo}"
            )
        todas = list(leitor)
        amostras = [
            linha for linha in todas if "fluxo completo" in linha[colunas["label"]].lower()
        ]
        # Cada fluxo deve encontrar o customer (200) enquanto aguarda o evento assíncrono.
        # Se quase nenhuma espera termina em 200, o JMX está consultando o id errado ou o
        # evento não chega: o fluxo "passa" (404 é ignorado) mas só estoura o tempo máximo.
        respostas_espera = [
            linha.get(colunas.get("responsecode", ""), "")
            for linha in todas
            if linha[colunas["label"]].strip().lower() == "aguardar customer"
        ]
        if respostas_espera:
            encontrados = sum(1 for codigo in respostas_espera if codigo == "200")
            if encontrados < 0.9 * len(amostras):
                raise RuntimeError(
                    f"Só {encontrados} de {len(amostras)} fluxos encontraram o customer "
                    f"(200) na espera; os demais estouraram o tempo máximo. "
                    f"Verifique o id extraído no sign-in e o envio do evento SignedUp: {arquivo}"
                )

    if not amostras:
        raise RuntimeError(f"Nenhum fluxo completo encontrado no aquecimento: {arquivo}")
    if len(amostras) <= 60:
        raise RuntimeError(
            f"Aquecimento gerou só {len(amostras)} fluxos (esperado >> 60). "
            f"O plano JMeter provavelmente não está repetindo iterações: {arquivo}"
        )
    falhas = [
        linha
        for linha in amostras
        if linha[colunas["success"]].strip().lower() not in {"true", "1"}
    ]
    if falhas:
        codigos = sorted(
            {
                linha.get(colunas.get("responsecode", ""), "desconhecido")
                for linha in falhas
            }
        )
        raise RuntimeError(
            f"Aquecimento inválido: {len(falhas)}/{len(amostras)} fluxos falharam "
            f"(códigos HTTP: {', '.join(codigos)}). Consulte {arquivo}."
        )


def executar_bateria(numero: int, jmeter: str, compose: list[str]) -> None:
    prefixo = f"bateria_{numero:02d}"
    arquivo_aquecimento = PASTA_RESULTADOS / f"{prefixo}_aquecimento.csv"
    log_aquecimento = PASTA_RESULTADOS / f"{prefixo}_aquecimento.log"
    arquivo_jmeter = PASTA_RESULTADOS / f"{prefixo}_jmeter.csv"
    log_jmeter = PASTA_RESULTADOS / f"{prefixo}_jmeter.log"
    log_aplicacao = PASTA_RESULTADOS / f"{prefixo}_aplicacao.log"
    arquivo_stats = PASTA_RESULTADOS / f"{prefixo}_docker_stats.csv"
    subida_iniciada = False
    falha: Exception | None = None
    parar_monitor = threading.Event()
    erros_monitor: list[str] = []
    monitor: threading.Thread | None = None

    print(f"Bateria Inflow microsserviços {numero}/{NUM_RODADAS}")
    try:
        subprocess.run(
            [*compose, "down", "--volumes", "--remove-orphans"],
            cwd=PASTA_PROJETO,
            check=False,
        )

        limpar_arquivos(
            arquivo_aquecimento,
            log_aquecimento,
            arquivo_jmeter,
            log_jmeter,
            arquivo_stats,
            log_aplicacao,
        )

        subida_iniciada = True
        subprocess.run(
            [*compose, "up", "-d"],
            cwd=PASTA_PROJETO,
            check=True,
        )
        aguardar_aplicacao()

        executar_jmeter(
            [
                jmeter,
                "-n",
                "-t",
                str(PLANO_JMETER),
                "-l",
                str(arquivo_aquecimento),
                "-j",
                str(log_aquecimento),
                f"-Jtest.duration={TEMPO_AQUECIMENTO}",
                "-Jjmeter.save.saveservice.output_format=csv",
                "-Jjmeter.save.saveservice.print_field_names=true",
            ]
        )
        validar_aquecimento(arquivo_aquecimento)

        monitor = threading.Thread(
            target=monitorar_docker,
            args=(obter_ids_containers(compose), arquivo_stats, parar_monitor, erros_monitor),
            name=f"monitor-inflow-microsservicos-{numero}",
        )
        monitor.start()
        executar_jmeter(
            [
                jmeter,
                "-n",
                "-t",
                str(PLANO_JMETER),
                "-l",
                str(arquivo_jmeter),
                "-j",
                str(log_jmeter),
                f"-Jtest.duration={TEMPO_MEDICAO}",
                "-Jjmeter.save.saveservice.output_format=csv",
                "-Jjmeter.save.saveservice.print_field_names=true",
            ]
        )
    except Exception as erro:
        falha = erro
    finally:
        parar_monitor.set()
        if monitor is not None:
            monitor.join()
        if erros_monitor and falha is None:
            falha = RuntimeError("Falha em docker stats: " + "; ".join(erros_monitor))
        if falha is not None and subida_iniciada:
            try:
                resultado_logs = subprocess.run(
                    [
                        *compose,
                        "logs",
                        "--no-color",
                        "--timestamps",
                        *SERVICOS_MONITORADOS,
                    ],
                    cwd=PASTA_PROJETO,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                resultado_ps = subprocess.run(
                    [*compose, "ps", "-a"],
                    cwd=PASTA_PROJETO,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                log_aplicacao.write_text(
                    "=== docker compose ps -a ===\n"
                    + resultado_ps.stdout
                    + resultado_ps.stderr
                    + "\n=== docker compose logs ===\n"
                    + (resultado_logs.stdout + resultado_logs.stderr)[-2_000_000:],
                    encoding="utf-8",
                )
            except OSError as erro_logs:
                print(f"Não foi possível salvar os logs do Compose: {erro_logs}")
        if subida_iniciada:
            try:
                subprocess.run(
                    [*compose, "down", "--volumes", "--remove-orphans"],
                    cwd=PASTA_PROJETO,
                    check=True,
                )
            except Exception as erro_limpeza:
                if falha is None:
                    falha = erro_limpeza
                else:
                    print(f"Falha adicional ao encerrar o Compose: {erro_limpeza}")

    if falha is not None:
        if log_aplicacao.is_file():
            falha = RuntimeError(f"{falha}; logs da aplicação: {log_aplicacao}")
        raise RuntimeError(f"Bateria {numero} dos microsserviços Inflow falhou: {falha}") from falha
    print(f"JMeter: {arquivo_jmeter}")
    print(f"Docker stats: {arquivo_stats}")


def main() -> None:
    if not ARQUIVO_COMPOSE.is_file():
        raise FileNotFoundError(f"Compose não encontrado: {ARQUIVO_COMPOSE}")
    if not PLANO_JMETER.is_file():
        raise FileNotFoundError(f"Plano JMeter não encontrado: {PLANO_JMETER}")
    if NUM_RODADAS < 1 or TEMPO_AQUECIMENTO < 1 or TEMPO_MEDICAO < 1:
        raise ValueError("Rodadas e durações devem ser maiores que zero.")
    if not shutil.which("docker"):
        raise FileNotFoundError("Docker não encontrado no PATH.")

    PASTA_RESULTADOS.mkdir(parents=True, exist_ok=True)
    jmeter = localizar_jmeter()
    compose = [
        "docker",
        "compose",
        "-f",
        str(ARQUIVO_COMPOSE),
        "--project-directory",
        str(PASTA_PROJETO),
    ]
    subprocess.run([*compose, "version"], cwd=PASTA_PROJETO, check=True)
    corrigir_dockerfiles()
    print("Construindo imagens Docker dos microsserviços (uma única vez)...")
    limpar_cache_buildkit()
    try:
        subprocess.run([*compose, "build"], cwd=PASTA_PROJETO, check=True)
    except subprocess.CalledProcessError:
        print("Build Docker falhou; limpando cache BuildKit e tentando novamente...")
        limpar_cache_buildkit()
        subprocess.run([*compose, "build"], cwd=PASTA_PROJETO, check=True)
    print(f"Aquecimento={TEMPO_AQUECIMENTO}s, medição={TEMPO_MEDICAO}s.")
    for numero in range(1, NUM_RODADAS + 1):
        executar_bateria(numero, jmeter, compose)


if __name__ == "__main__":
    main()
