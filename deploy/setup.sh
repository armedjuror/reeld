#!/usr/bin/env bash
# ReelD — EC2 one-time server setup
# Run as: sudo bash setup.sh
# Tested on Ubuntu 22.04 LTS

set -euo pipefail

APP_USER="reeld"
APP_DIR="/opt/reeld"
PYTHON="python3.10"

echo "==> Updating packages"
apt-get update -qq
apt-get upgrade -y -qq

echo "==> Installing system dependencies"
apt-get install -y -qq \
  python3 python3-venv python3-dev \
  python3-pip \
  ffmpeg \
  nginx \
  certbot python3-certbot-nginx \
  git \
  build-essential \
  libpq-dev \
  curl \
  unzip

echo "==> Creating app user"
id -u $APP_USER &>/dev/null || useradd --system --create-home --shell /bin/bash $APP_USER

echo "==> Creating app directory"
mkdir -p $APP_DIR
chown $APP_USER:$APP_USER $APP_DIR

echo "==> Cloning / copying app"
# If deploying via git:
#   git clone https://github.com/armedjuror/ReelD $APP_DIR
# If deploying via rsync (from your machine, run separately):
#   rsync -avz --exclude venv --exclude __pycache__ --exclude .env \
#     ./ ec2-user@<IP>:/opt/reeld/
echo "  [!] Copy your app files to $APP_DIR before continuing"
echo "      e.g.: rsync -avz --exclude venv --exclude __pycache__ --exclude .env ./ ubuntu@<IP>:$APP_DIR/"
echo ""

echo "==> Creating Python virtualenv"
sudo -u $APP_USER $PYTHON -m venv $APP_DIR/venv

echo "==> Installing Python dependencies"
sudo -u $APP_USER $APP_DIR/venv/bin/pip install --upgrade pip -q
sudo -u $APP_USER $APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt -q

echo "==> Installing local Whisper (openai-whisper + torch CPU)"
# torch CPU wheel is much smaller than the CUDA version (~700MB vs 2GB)
sudo -u $APP_USER $APP_DIR/venv/bin/pip install \
  openai-whisper \
  torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu -q

echo "==> Pre-downloading Whisper 'base' model (avoids first-request delay)"
sudo -u $APP_USER bash -c "
  source $APP_DIR/venv/bin/activate
  python -c \"import whisper; whisper.load_model('base')\"
"

echo "==> Creating required directories"
for dir in temp outputs reel_assets fonts backgrounds bgm frames; do
  sudo -u $APP_USER mkdir -p $APP_DIR/$dir
done

echo "==> Installing systemd service"
cp $APP_DIR/deploy/reeld.service /etc/systemd/system/reeld.service
systemctl daemon-reload
systemctl enable reeld

echo "==> Installing nginx config"
cp $APP_DIR/deploy/nginx.conf /etc/nginx/sites-available/reeld
ln -sf /etc/nginx/sites-available/reeld /etc/nginx/sites-enabled/reeld
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo ""
echo "==> Done. Next steps:"
echo "  1. Copy your .env to $APP_DIR/.env  (chmod 600)"
echo "  2. Update GOOGLE_REDIRECT_URI in .env to your domain"
echo "  3. Edit /etc/nginx/sites-available/reeld — set your domain"
echo "  4. Run: certbot --nginx -d yourdomain.com  (for HTTPS)"
echo "  5. systemctl start reeld"
echo "  6. systemctl status reeld"
