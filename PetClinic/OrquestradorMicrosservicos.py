import csv
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


NUM_RODADAS = 30
TEMPO_AQUECIMENTO = 60
TEMPO_MEDICAO = 120
TEMPO_MAXIMO_INICIALIZACAO = 300
INTERVALO_MONITORAMENTO = 1
JMETER_HOME: Path | None = Path(r"C:\apache-jmeter-5.6.3")

PASTA_EXPERIMENTO = Path(__file__).resolve().parent
PASTA_PROJETO = PASTA_EXPERIMENTO / "spring-petclinic-microservices"
ARQUIVO_COMPOSE = PASTA_PROJETO / "docker-compose.yml"
PLANO_JMETER = (
    PASTA_PROJETO
    / "spring-petclinic-api-gateway"
    / "src"
    / "test"
    / "jmeter"
    / "PetClinicMicrosservicosControlado.jmx"
)
PASTA_RESULTADOS = PASTA_EXPERIMENTO / "resultados_comparacao" / "microsservicos"
URL_BASE = "http://localhost:8080"
URL_PRONTIDAO_VETS = f"{URL_BASE}/api/vets"
URL_PRONTIDAO_OWNERS = f"{URL_BASE}/api/owners"
SERVICOS_MONITORADOS = (
    "api-gateway",
    "customers-service",
    "vets-service",
    "postgres",
)
MODULOS_DOCKER = (
    "spring-petclinic-customers-service",
    "spring-petclinic-vets-service",
    "spring-petclinic-api-gateway",
)


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


def executar_jmeter(comando: list[str]) -> None:
    executar_comando(comando, call=True)


def _http_json(method: str, url: str, corpo: dict | None = None) -> dict | list:
    dados = None if corpo is None else json.dumps(corpo).encode("utf-8")
    requisicao = Request(
        url,
        data=dados,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urlopen(requisicao, timeout=10) as resposta:
        bruto = resposta.read().decode("utf-8")
        return json.loads(bruto) if bruto else {}


def _fluxo_prontidao() -> None:
    """Exercita o mesmo caminho do JMeter: vets + criar dono + criar pet."""
    _http_json("GET", URL_PRONTIDAO_VETS)
    dono = _http_json(
        "POST",
        URL_PRONTIDAO_OWNERS,
        {
            "firstName": "Warmup",
            "lastName": "Ready",
            "address": "1 Ready St",
            "city": "City",
            "telephone": "1199999999",
        },
    )
    dono_id = dono["id"]
    _http_json(
        "POST",
        f"{URL_PRONTIDAO_OWNERS}/{dono_id}/pets",
        {"name": "ReadyPet", "birthDate": "2020-01-15", "typeId": 2},
    )


def aguardar_aplicacao() -> None:
    limite = time.monotonic() + TEMPO_MAXIMO_INICIALIZACAO
    ultimo_erro: Exception | None = None
    while time.monotonic() < limite:
        try:
            _fluxo_prontidao()
            time.sleep(5)
            return
        except (OSError, URLError, HTTPError, TimeoutError, KeyError, json.JSONDecodeError) as erro:
            ultimo_erro = erro
            time.sleep(2)
    raise TimeoutError(
        "Stack enxuta indisponível para o fluxo completo "
        f"(vets/owners/pets) em {URL_BASE}; último erro: {ultimo_erro}"
    )


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
        raise RuntimeError("Nenhum container de aplicação foi encontrado para monitorar.")
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
        amostras = [
            linha
            for linha in leitor
            if "fluxo completo" in linha[colunas["label"]].lower()
        ]

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

    print(f"Bateria microsserviços {numero}/{NUM_RODADAS}")
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
            name=f"monitor-microsservicos-{numero}",
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
                log_aplicacao.write_text(
                    (resultado_logs.stdout + resultado_logs.stderr)[-2_000_000:],
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
            falha = RuntimeError(f"{falha}; logs dos serviços: {log_aplicacao}")
        raise RuntimeError(
            f"Bateria {numero} de microsserviços falhou: {falha}"
        ) from falha
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
    mvnw = PASTA_PROJETO / ("mvnw.cmd" if os.name == "nt" else "mvnw")
    if not mvnw.is_file():
        raise FileNotFoundError(f"Maven Wrapper não encontrado: {mvnw}")
    print("Construindo imagens Docker locais da stack enxuta...")
    build_cmd = [
        str(mvnw),
        "-q",
        "clean",
        "install",
        "-Dmaven.test.skip=true",
        "-PbuildDocker",
        "-pl",
        ",".join(MODULOS_DOCKER),
        "-am",
    ]
    limpar_cache_buildkit()
    try:
        executar_comando(build_cmd, call=True)
    except subprocess.CalledProcessError:
        print("Build Docker falhou; limpando cache BuildKit e tentando novamente...")
        limpar_cache_buildkit()
        executar_comando(build_cmd, call=True)
    print(f"Aquecimento={TEMPO_AQUECIMENTO}s, medição={TEMPO_MEDICAO}s.")
    for numero in range(1, NUM_RODADAS + 1):
        executar_bateria(numero, jmeter, compose)


if __name__ == "__main__":
    main()
