include $(TOPDIR)/rules.mk

LUCI_TITLE:=LuCI app for Dejede OLEDMON (SSD1306 128x64)
LUCI_DEPENDS:=+luci-base
LUCI_PKGARCH:=all

PKG_VERSION:=1.0.0
PKG_RELEASE:=1
PKG_LICENSE:=MIT

include $(TOPDIR)/feeds/luci/luci.mk

# call BuildPackage - OpenWrt buildroot signature
