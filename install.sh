#!/bin/sh
# Optional: the plugin fetches AFC itself at Klipper start. This does it by
# hand: fetch AFC-Klipper-Add-On (a git submodule of this plugin) at the
# tested commit and create AFC's config folder. Run as the klipper user
# (Kiln: plugins and printer_data belong to klipper):
#   su -s /bin/sh klipper -c "sh /data/ad5x/plugins/afc/install.sh"
set -e
cd "$(dirname "$0")"
AFC_REPO=https://github.com/AFCProject/AFC-Klipper-Add-On.git
AFC_COMMIT=$(cat AFC_COMMIT)
if [ ! -e AFC-Klipper-Add-On/.git ]; then
    git submodule update --init --quiet -- AFC-Klipper-Add-On ||
        git clone --quiet "$AFC_REPO" AFC-Klipper-Add-On
fi
git -C AFC-Klipper-Add-On remote set-url origin "$AFC_REPO"
git -C AFC-Klipper-Add-On checkout --quiet "$AFC_COMMIT" 2>/dev/null || {
    git -C AFC-Klipper-Add-On fetch --quiet origin
    git -C AFC-Klipper-Add-On checkout --quiet "$AFC_COMMIT"
}
mkdir -p /home/klipper/printer_data/config/AFC
echo "AFC $(git -C AFC-Klipper-Add-On rev-parse --short HEAD) ready; restart Klipper"
