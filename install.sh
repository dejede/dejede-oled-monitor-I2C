#!/bin/sh
# Pasang langsung ke router tanpa SDK.
# Cara: salin folder ini ke router (mis. /tmp), lalu:  sh install.sh
cd "$(dirname "$0")" || exit 1
set -e

# 1) skrip utama + LuCI
mkdir -p /usr/bin /usr/share/luci/menu.d /usr/share/rpcd/acl.d
cp root/usr/bin/oled_monitor.py /usr/bin/oled_monitor.py
chmod +x /usr/bin/oled_monitor.py
cp root/usr/share/luci/menu.d/*.json /usr/share/luci/menu.d/
cp root/usr/share/rpcd/acl.d/*.json /usr/share/rpcd/acl.d/
mkdir -p /www/luci-static/resources/view/oled_monitor
cp htdocs/luci-static/resources/view/oled_monitor/settings.js /www/luci-static/resources/view/oled_monitor/

# 2) init script: hanya dipasang jika belum ada
if [ ! -f /etc/init.d/oled_monitor ]; then
	cp root/etc/init.d/oled_monitor /etc/init.d/oled_monitor
	chmod +x /etc/init.d/oled_monitor
fi

# 3) konfigurasi: konfigurasi lama (tanpa section 'global') dicadangkan lalu diganti default baru
if [ -f /etc/config/oled_monitor ] && ! grep -q "config global" /etc/config/oled_monitor; then
	cp /etc/config/oled_monitor /etc/config/oled_monitor.bak
	echo "Konfigurasi lama dicadangkan ke /etc/config/oled_monitor.bak"
	rm /etc/config/oled_monitor
fi
[ -f /etc/config/oled_monitor ] || cp root/etc/config/oled_monitor /etc/config/oled_monitor

# 4) segarkan LuCI & jalankan layanan
rm -rf /tmp/luci-indexcache /tmp/luci-modulecache 2>/dev/null || true
/etc/init.d/rpcd restart
/etc/init.d/oled_monitor enable
/etc/init.d/oled_monitor restart
echo "Selesai. Buka LuCI > Services > Dejede OLEDMON."
