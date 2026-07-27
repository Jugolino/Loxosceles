#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Criando ambiente virtual em $DIR/.venv"
python3 -m venv "$DIR/.venv"

echo "==> Instalando dependencias Python"
"$DIR/.venv/bin/pip" install --upgrade pip -q
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt" -q

# Prefere /usr/local/bin (ja fica no PATH de qualquer shell, igual nmap/curl/etc).
# So cai para ~/.local/bin se nao tiver permissao de escrita (ex: sem root/sudo).
if [ -w /usr/local/bin ]; then
    BIN_DIR="/usr/local/bin"
else
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$BIN_DIR"
fi

echo "==> Instalando comando global 'Loxosceles' em $BIN_DIR"
cat > "$BIN_DIR/Loxosceles" <<EOF
#!/usr/bin/env bash
exec "$DIR/.venv/bin/python" "$DIR/loxosceles.py" "\$@"
EOF
chmod +x "$BIN_DIR/Loxosceles"

# --- gau e waybackurls: mais fontes de dados, instalados automaticamente ---

install_go_tool() {
    local name="$1" pkg="$2"
    if command -v "$name" >/dev/null 2>&1; then
        echo "==> $name ja esta instalado, pulando"
        return
    fi
    echo "==> Instalando $name..."
    if GOBIN="$BIN_DIR" go install "$pkg" 2>&1 | tail -5; then
        echo "==> $name instalado em $BIN_DIR"
    else
        echo "AVISO: falha ao instalar $name. Loxosceles funciona sem ele (cai para a CDX API direto)."
    fi
}

if ! command -v go >/dev/null 2>&1; then
    echo "==> Go nao encontrado (necessario para instalar gau/waybackurls), tentando instalar..."
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq && apt-get install -y -qq golang-go || \
            echo "AVISO: nao consegui instalar Go automaticamente. Instale manualmente e rode este script de novo para ganhar gau/waybackurls."
    else
        echo "AVISO: gerenciador de pacotes nao suportado para auto-instalar Go. Instale manualmente se quiser gau/waybackurls."
    fi
fi

if command -v go >/dev/null 2>&1; then
    install_go_tool gau github.com/lc/gau/v2/cmd/gau@latest
    install_go_tool waybackurls github.com/tomnomnom/waybackurls@latest
fi

echo
echo "Instalado! Rode 'Loxosceles seudominio.com' de qualquer lugar."

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo
        echo "AVISO: $BIN_DIR nao esta no seu PATH."
        echo "Adicione ao seu ~/.bashrc ou ~/.zshrc:"
        echo "  export PATH=\"$BIN_DIR:\$PATH\""
        ;;
esac
