#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"

echo "==> Criando ambiente virtual em $DIR/.venv"
python3 -m venv "$DIR/.venv"

echo "==> Instalando dependencias"
"$DIR/.venv/bin/pip" install --upgrade pip -q
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt" -q

mkdir -p "$BIN_DIR"
LAUNCHER="$BIN_DIR/Loxosceles"

echo "==> Instalando comando global em $LAUNCHER"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$DIR/.venv/bin/python" "$DIR/loxosceles.py" "\$@"
EOF
chmod +x "$LAUNCHER"

echo
echo "Instalado! Rode 'Loxosceles seudominio.com' de qualquer lugar."

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo
        echo "AVISO: $BIN_DIR nao esta no seu PATH."
        echo "Adicione ao seu ~/.bashrc ou ~/.zshrc:"
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac
