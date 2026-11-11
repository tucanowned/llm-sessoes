"""Leitura de /proc. Tudo recebe a raiz como parâmetro para ser testável com diretórios falsos."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROC = Path("/proc")


@dataclass(frozen=True)
class Processo:
    pid: int
    comm: str
    starttime: str
    tty: str | None
    cmdline: list[str]
    cwd: str | None


def boot_id(proc: Path = PROC) -> str:
    return (proc / "sys/kernel/random/boot_id").read_text().strip()


def ler_processo(pid: int, proc: Path = PROC) -> Processo | None:
    """Devolve None se o processo não existe (ou sumiu no meio da leitura)."""
    base = proc / str(pid)
    try:
        stat = (base / "stat").read_text()
        cmdline = (base / "cmdline").read_bytes()
    except OSError:
        return None

    # O comm vem entre parênteses e pode conter espaços/parênteses: parte-se pelo ÚLTIMO ')'.
    abre, fecha = stat.index("("), stat.rindex(")")
    comm = stat[abre + 1 : fecha]
    resto = stat[fecha + 2 :].split()  # resto[0] é o campo 3 (state)
    tty_nr, starttime = int(resto[4]), resto[19]

    try:
        cwd = str((base / "cwd").readlink())
    except OSError:
        cwd = None

    return Processo(
        pid=pid,
        comm=comm,
        starttime=starttime,
        tty=_nome_tty(tty_nr),
        cmdline=[p.decode(errors="replace") for p in cmdline.split(b"\0") if p],
        cwd=cwd,
    )


def arquivos_abertos(pid: int, proc: Path = PROC) -> list[str]:
    """Destinos dos descritores abertos pelo processo (vazio se não der para ler)."""
    try:
        entradas = list((proc / str(pid) / "fd").iterdir())
    except OSError:
        return []
    destinos = []
    for entrada in entradas:
        try:
            destinos.append(str(entrada.readlink()))
        except OSError:
            continue  # descritor fechado no meio da leitura
    return destinos


def ler_cauda(arquivo: Path, quantos_bytes: int) -> list[str]:
    """Últimas linhas de um arquivo grande; a primeira costuma vir cortada."""
    with arquivo.open("rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - quantos_bytes))
        return f.read().decode(errors="replace").splitlines()


def listar_processos(comm: str, proc: Path = PROC) -> list[Processo]:
    encontrados = []
    for entrada in proc.iterdir():
        if not entrada.name.isdigit():
            continue
        processo = ler_processo(int(entrada.name), proc)
        if processo and processo.comm == comm:
            encontrados.append(processo)
    return encontrados


def _nome_tty(tty_nr: int) -> str | None:
    if tty_nr == 0:
        return None
    major = (tty_nr >> 8) & 0xFFF
    minor = (tty_nr & 0xFF) | ((tty_nr >> 12) & 0xFFF00)
    if 136 <= major <= 143:  # faixa dos pseudo-terminais (pts)
        return f"pts/{(major - 136) * 256 + minor}"
    return f"tty{major}:{minor}"
