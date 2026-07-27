#!/usr/bin/env python3
"""
Loxosceles - descobre rotas esquecidas de um site usando arquivos historicos
(gau, waybackurls ou a CDX API do Wayback Machine diretamente) e verifica ao vivo
(curl) se ainda estao acessiveis.

Fonte dos dados, em cascata automatica: gau (wayback + commoncrawl + otx + urlscan)
se instalado -> waybackurls (so wayback) se instalado -> busca manual na CDX API.

Uso:
    Loxosceles exemplo.com
    Loxosceles https://exemplo.com --limit 3000 --concurrency 25 --no-probe
    Loxosceles exemplo.com --source waybackurls
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, Input, DataTable, RichLog, Static
from textual.reactive import reactive
from textual import work

# ---------------------------------------------------------------------------
# Regras de deteccao de rotas "interessantes"
# ---------------------------------------------------------------------------

INTERESTING_PATTERNS = [
    r"\.env(\.|$)", r"\.git(/|$)", r"\.svn(/|$)", r"config", r"\.sql$", r"\.bak$",
    r"\.zip$", r"\.tar(\.gz)?$", r"\.7z$", r"backup", r"admin", r"login", r"phpmyadmin",
    r"wp-admin", r"wp-login", r"swagger", r"actuator", r"debug", r"dashboard", r"api/",
    r"\.htpasswd", r"id_rsa", r"\.aws", r"credentials", r"secret", r"upload", r"staging",
    r"internal", r"test", r"\.log$", r"docker", r"\.yml$", r"\.yaml$", r"private",
]
INTERESTING_RE = re.compile("|".join(INTERESTING_PATTERNS), re.IGNORECASE)

CURL_UA = "Mozilla/5.0 (Loxosceles; +pentest-tool)"


@dataclass
class Route:
    path: str
    url: str
    status: str = "..."
    interesting: bool = False


def status_style(status: str, interesting: bool) -> str:
    if status in ("...", ""):
        return "dim"
    if status.startswith("2"):
        return "bold red" if interesting else "bold green"
    if status.startswith("3"):
        return "yellow"
    if status.startswith("4"):
        return "grey58"
    return "dim red"


def normalize_domain(raw: str) -> str:
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return urlparse(raw).netloc


async def run_cmd(*args: str, timeout: float = 30.0, input_data: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if input_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdin_bytes = input_data.encode() if input_data is not None else None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=stdin_bytes), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "", "timeout"
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def detect_source_tool(preferred: str = "auto") -> str:
    """Decide qual ferramenta usar para coletar URLs: gau (mais fontes),
    waybackurls (so wayback, mas leve) ou o fetch manual embutido (fallback)."""
    if preferred != "auto":
        return preferred
    if shutil.which("gau"):
        return "gau"
    if shutil.which("waybackurls"):
        return "waybackurls"
    return "wayback"


class LoxoscelesApp(App):
    CSS = """
    Screen {
        background: $surface;
    }
    #body {
        height: 1fr;
    }
    #left, #right {
        width: 1fr;
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    #left {
        border-title-color: $success;
    }
    #right {
        border-title-color: $warning;
    }
    #stats {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    DataTable {
        height: 1fr;
    }
    RichLog {
        height: 1fr;
    }
    Input {
        margin: 0 1;
    }
    """

    TITLE = "Loxosceles"
    SUB_TITLE = "descoberta de rotas esquecidas via Wayback Machine"

    BINDINGS = [
        ("q", "quit", "Sair"),
        ("s", "save", "Salvar resultados"),
        ("r", "restart", "Nova busca"),
        ("p", "toggle_probe", "Ligar/desligar verificacao"),
    ]

    total_found = reactive(0)
    total_verified = reactive(0)
    total_interesting = reactive(0)

    def __init__(self, domain: str | None, limit: int, concurrency: int, probe: bool, source: str = "auto"):
        super().__init__()
        self.domain = domain
        self.limit = limit
        self.concurrency = concurrency
        self.probe_enabled = probe
        self.source = source
        self.active_source = "-"
        self.routes: dict[str, Route] = {}
        self._row_keys: dict[str, object] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        if not self.domain:
            yield Input(placeholder="Digite o dominio alvo (ex: exemplo.com) e pressione Enter", id="domain_input")
        yield Static(self._stats_text(), id="stats")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield DataTable(id="routes_table")
            with Vertical(id="right"):
                yield RichLog(id="activity", wrap=True, highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        left = self.query_one("#left")
        right = self.query_one("#right")
        left.border_title = "ROTAS ENCONTRADAS"
        right.border_title = "ATIVIDADE"

        table = self.query_one("#routes_table", DataTable)
        table.add_column("Rota", key="path", width=60)
        table.add_column("Status", key="status", width=10)
        table.add_column("!", key="note", width=3)
        table.cursor_type = "row"
        table.zebra_stripes = True

        if self.domain:
            self.start_scan(self.domain)
        else:
            self.query_one("#domain_input", Input).focus()

    def _stats_text(self) -> str:
        probe_state = "ligada" if self.probe_enabled else "desligada"
        return (
            f" alvo: {self.domain or '-'}  |  fonte: {self.active_source}  |  rotas: {self.total_found}  |  "
            f"verificadas: {self.total_verified}  |  interessantes: {self.total_interesting}  |  "
            f"verificacao: {probe_state}"
        )

    def _refresh_stats(self) -> None:
        try:
            self.query_one("#stats", Static).update(self._stats_text())
        except Exception:
            pass

    def watch_total_found(self, _):
        self._refresh_stats()

    def watch_total_verified(self, _):
        self._refresh_stats()

    def watch_total_interesting(self, _):
        self._refresh_stats()

    def log_activity(self, msg: str, style: str = "") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        log = self.query_one("#activity", RichLog)
        if style:
            log.write(f"[dim]{ts}[/] [{style}]{msg}[/{style}]")
        else:
            log.write(f"[dim]{ts}[/] {msg}")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "domain_input":
            domain = event.value.strip()
            if not domain:
                return
            event.input.remove()
            self.domain = normalize_domain(domain)
            self.start_scan(self.domain)

    def start_scan(self, domain: str) -> None:
        self.domain = normalize_domain(domain)
        self._refresh_stats()
        self.run_scan()

    @work(exclusive=True)
    async def run_scan(self) -> None:
        table = self.query_one("#routes_table", DataTable)
        table.clear()
        self.routes.clear()
        self._row_keys.clear()
        self.total_found = 0
        self.total_verified = 0
        self.total_interesting = 0

        domain = self.domain
        self.active_source = detect_source_tool(self.source)
        self._refresh_stats()

        urls = await self.fetch_urls(domain)
        if urls is None:
            return
        seen_paths: dict[str, str] = {}
        for full_url in urls:
            parsed = urlparse(full_url)
            path = parsed.path or "/"  # agrupa por path, ignora query para exibicao de "rota"
            if path not in seen_paths:
                seen_paths[path] = full_url

        self.log_activity(f"[bold green]{len(seen_paths)}[/bold green] rotas unicas encontradas ({len(urls)} URLs brutas no total).")

        for path, url in sorted(seen_paths.items()):
            interesting = bool(INTERESTING_RE.search(path))
            route = Route(path=path, url=url, interesting=interesting)
            self.routes[path] = route
            note = "⚠" if interesting else ""
            style = "bold red" if interesting else ""
            display_path = path if len(path) <= 58 else path[:57] + "…"
            row_key = table.add_row(
                f"[{style}]{display_path}[/{style}]" if style else display_path,
                "...",
                note,
                key=path,
            )
            self._row_keys[path] = row_key
            if interesting:
                self.total_interesting += 1

        self.total_found = len(seen_paths)

        if self.probe_enabled:
            await self.probe_all()
        else:
            self.log_activity("Verificacao ao vivo desligada (tecla 'p' para ligar). Apenas listando rotas.", "dim")

    async def fetch_urls(self, domain: str) -> list[str] | None:
        """Coleta as URLs historicas do dominio, em cascata: gau (wayback +
        commoncrawl + otx + urlscan) -> waybackurls (so wayback) -> fetch
        manual da CDX API. So cai para o proximo se o anterior nao estiver
        instalado ou falhar."""
        tool = self.active_source

        if tool == "gau":
            urls = await self.fetch_via_gau(domain)
            if urls:
                return urls
            self.log_activity("gau nao retornou resultados, tentando waybackurls...", "yellow")
            tool = "waybackurls" if shutil.which("waybackurls") else "wayback"
            self.active_source = tool
            self._refresh_stats()

        if tool == "waybackurls":
            urls = await self.fetch_via_waybackurls(domain)
            if urls:
                return urls
            self.log_activity("waybackurls nao retornou resultados, tentando busca manual na CDX API...", "yellow")
            tool = "wayback"
            self.active_source = tool
            self._refresh_stats()

        return await self.fetch_cdx_urls(domain)

    async def fetch_via_gau(self, domain: str) -> list[str] | None:
        self.log_activity(f"Usando [bold]gau[/bold] (wayback + commoncrawl + otx + urlscan) para [bold]{domain}[/bold]...")
        cmd = ["gau", "--subs", "--threads", str(max(self.concurrency, 5))]
        self.log_activity(f"$ echo {domain} | {' '.join(cmd)}", "dim cyan")
        code, out, err = await run_cmd(*cmd, input_data=domain, timeout=150.0)
        if code != 0 or not out.strip():
            self.log_activity(f"gau falhou: {err.strip().splitlines()[-1] if err.strip() else 'sem resposta'}", "yellow")
            return None
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def fetch_via_waybackurls(self, domain: str) -> list[str] | None:
        self.log_activity(f"Usando [bold]waybackurls[/bold] para [bold]{domain}[/bold]...")
        cmd = ["waybackurls"]
        self.log_activity(f"$ echo {domain} | {' '.join(cmd)}", "dim cyan")
        code, out, err = await run_cmd(*cmd, input_data=domain, timeout=120.0)
        if code != 0 or not out.strip():
            self.log_activity(f"waybackurls falhou: {err.strip().splitlines()[-1] if err.strip() else 'sem resposta'}", "yellow")
            return None
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def fetch_cdx_urls(self, domain: str) -> list[str] | None:
        """Busca manual na CDX API do Wayback Machine (usada quando gau e
        waybackurls nao estao instalados). Tenta primeiro com collapse=urlkey
        (mais leve para transferir), e se a API travar/der 504 (comum em
        dominios grandes), cai para uma busca sem collapse com dedup no cliente."""
        self.log_activity(f"Consultando Wayback Machine (CDX API) diretamente para [bold]{domain}[/bold]...")

        collapsed_url = (
            "https://web.archive.org/cdx/search/cdx"
            f"?url={domain}/*&output=json&collapse=urlkey&fl=original&limit={min(self.limit, 5000)}"
        )
        self.log_activity(f"$ curl -s -A '...' -m 45 \"{collapsed_url}\"", "dim cyan")
        code, out, err = await run_cmd("curl", "-s", "-A", CURL_UA, "-m", "45", collapsed_url, timeout=50.0)

        rows = self._parse_cdx_output(code, out, err, quiet=True)
        if rows is not None:
            return [row[0] for row in rows]

        self.log_activity("Modo rapido (collapse) indisponivel/lento, tentando modo alternativo sem collapse...", "yellow")

        fallback_url = (
            "https://web.archive.org/cdx/search/cdx"
            f"?url={domain}/*&output=json&fl=original&limit={self.limit}"
        )
        self.log_activity(f"$ curl -s -A '...' -m 90 \"{fallback_url}\"", "dim cyan")
        code, out, err = await run_cmd("curl", "-s", "-A", CURL_UA, "-m", "90", fallback_url, timeout=95.0)

        rows = self._parse_cdx_output(code, out, err, quiet=False)
        return [row[0] for row in rows] if rows is not None else None

    def _parse_cdx_output(self, code: int, out: str, err: str, quiet: bool) -> list | None:
        if code != 0 or not out.strip():
            if not quiet:
                self.log_activity(f"Falha ao consultar o Wayback Machine: {err or 'sem resposta'}", "bold red")
            return None

        stripped = out.strip()
        if stripped.startswith("<"):
            if not quiet:
                self.log_activity("Wayback Machine respondeu com erro (ex: 504 Gateway Timeout).", "bold red")
            return None

        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            if not quiet:
                self.log_activity("Resposta invalida da CDX API.", "bold red")
            return None

        if len(data) <= 1:
            if not quiet:
                self.log_activity("Nenhum snapshot encontrado para esse dominio.", "yellow")
            return None

        return data[1:]  # primeira linha e o cabecalho ["original"]

    async def probe_all(self) -> None:
        self.log_activity(f"Iniciando verificacao ao vivo com curl (concorrencia={self.concurrency})...")
        sem = asyncio.Semaphore(self.concurrency)
        tasks = [self.probe_one(sem, path, route.url) for path, route in self.routes.items()]
        await asyncio.gather(*tasks)
        self.log_activity("[bold green]Verificacao concluida.[/bold green]")

    async def probe_one(self, sem: asyncio.Semaphore, path: str, url: str) -> None:
        async with sem:
            cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-I",
                   "-A", CURL_UA, "-m", "8", "-k", url]
            self.log_activity(f"$ {' '.join(cmd[:-1])} {url}", "dim cyan")
            code, out, err = await run_cmd(*cmd, timeout=10.0)
            status = out.strip() if code == 0 and out.strip() else "erro"

            route = self.routes.get(path)
            if route:
                route.status = status

            interesting = self.routes[path].interesting
            style = status_style(status, interesting)
            result_style = "bold red" if (interesting and status.startswith("2")) else style
            self.log_activity(f"  -> {path} = {status}", result_style)

            table = self.query_one("#routes_table", DataTable)
            row_key = self._row_keys.get(path)
            if row_key is not None:
                try:
                    table.update_cell(row_key, "status", f"[{style}]{status}[/{style}]")
                except Exception:
                    pass
            self.total_verified += 1

    def action_toggle_probe(self) -> None:
        self.probe_enabled = not self.probe_enabled
        self._refresh_stats()
        self.log_activity(f"Verificacao ao vivo {'ligada' if self.probe_enabled else 'desligada'}.")

    def action_restart(self) -> None:
        self.domain = None
        left = self.query_one("#left")
        table = self.query_one("#routes_table", DataTable)
        table.clear()
        self.routes.clear()
        self._row_keys.clear()
        self.total_found = 0
        self.total_verified = 0
        self.total_interesting = 0
        self.log_activity("Pronto para nova busca.")
        stats = self.query_one("#stats")
        body = self.query_one("#body")
        existing = self.query("#domain_input")
        if not existing:
            inp = Input(placeholder="Digite o dominio alvo (ex: exemplo.com) e pressione Enter", id="domain_input")
            self.mount(inp, before=stats)
            inp.focus()

    def action_save(self) -> None:
        if not self.routes:
            self.log_activity("Nada para salvar ainda.", "yellow")
            return
        out_dir = Path(__file__).parent / "results"
        out_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_domain = (self.domain or "dominio").replace("/", "_")
        out_file = out_dir / f"{safe_domain}_{ts}.json"
        payload = [
            {"path": r.path, "url": r.url, "status": r.status, "interesting": r.interesting}
            for r in sorted(self.routes.values(), key=lambda r: r.path)
        ]
        out_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        self.log_activity(f"[bold green]Resultados salvos em {out_file}[/bold green]")


def main() -> None:
    parser = argparse.ArgumentParser(prog="Loxosceles", description="Loxosceles - descobre rotas esquecidas de um site via arquivos historicos (gau/waybackurls/Wayback Machine)")
    parser.add_argument("domain", nargs="?", help="Dominio alvo, ex: exemplo.com")
    parser.add_argument("--limit", type=int, default=20000, help="Limite de snapshots a buscar na CDX API (default: 20000)")
    parser.add_argument("--concurrency", type=int, default=20, help="Requisicoes curl simultaneas (default: 20)")
    parser.add_argument("--no-probe", action="store_true", help="Nao verificar rotas ao vivo, so listar")
    parser.add_argument(
        "--source", choices=["auto", "gau", "waybackurls", "wayback"], default="auto",
        help="Fonte de coleta: auto detecta gau/waybackurls instalados e usa o melhor disponivel, "
             "com fallback para busca manual na CDX API (default: auto)",
    )
    args = parser.parse_args()

    app = LoxoscelesApp(
        domain=normalize_domain(args.domain) if args.domain else None,
        limit=args.limit,
        concurrency=args.concurrency,
        probe=not args.no_probe,
        source=args.source,
    )
    app.run()


if __name__ == "__main__":
    main()
