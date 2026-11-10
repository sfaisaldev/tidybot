#!/usr/bin/env bash
# TidyBot installer for Ubuntu.  Usage:  ./install.sh  [--autostart]
set -e
cd "$(dirname "$0")"

echo "Checking dependencies (usually already installed on Ubuntu)..."
if ! python3 -c "import gi; gi.require_version('Gtk','3.0'); import cairo; from gi.repository import Gtk" 2>/dev/null; then
  sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0
fi

mkdir -p "$HOME/.local/share/tidybot" "$HOME/.local/bin"
cp tidybot.py "$HOME/.local/share/tidybot/tidybot.py"

cat > "$HOME/.local/bin/tidybot" <<'EOF'
#!/usr/bin/env bash
exec python3 "$HOME/.local/share/tidybot/tidybot.py" "$@"
EOF
chmod +x "$HOME/.local/bin/tidybot"

if [ "$1" = "--autostart" ]; then
  mkdir -p "$HOME/.config/autostart"
  cat > "$HOME/.config/autostart/tidybot.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=TidyBot
Comment=A robot that keeps your desktop tidy
Exec=python3 $HOME/.local/share/tidybot/tidybot.py
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=8
EOF
  echo "TidyBot will start automatically when you log in."
fi

echo
echo "Installed. Try it:"
echo "  tidybot             (then drag an icon somewhere and wait ~3 seconds)"
echo "  tidybot --demo      (adds 4 sample files at random spots)"
echo "  tidybot --debug     (shows what it sees, if it aims wrong)"
echo "  tidybot --pause / --stop"
echo "If 'tidybot' isn't found, log out and back in (so ~/.local/bin is on PATH)."
