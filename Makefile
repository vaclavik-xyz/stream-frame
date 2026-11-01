PREFIX      ?= $(HOME)/stream-frame
UPSTREAM    ?= http://127.0.0.1:8101
CC          ?= cc
CFLAGS      ?= -O2 -fPIC -Wall -Wextra -shared
VULKAN_INC  ?= /usr/include
PY          ?= python3

LAYER_SRC   := layer/vrcap.c
LAYER_OUT   := build/libvrcap.so
MANIFEST    := layer/vrcap_layer.json

.PHONY: all layer test lint run install install-layer install-units clean

all: layer

# ---------------------------------------------------------------- layer
layer: $(LAYER_OUT)

$(LAYER_OUT): $(LAYER_SRC)
	@test -f "$(VULKAN_INC)/vulkan/vulkan.h" || { \
	  echo "vulkan headers not found in $(VULKAN_INC)"; \
	  echo "hint: git clone --depth 1 https://github.com/KhronosGroup/Vulkan-Headers /tmp/vkh"; \
	  echo "      make layer VULKAN_INC=/tmp/vkh/include"; exit 1; }
	@mkdir -p build
	$(CC) $(CFLAGS) -I$(VULKAN_INC) -o $@ $< -ldl
	@echo "built $@"

# Vulkan headers missing? SteamOS and most distros ship them; otherwise:
#   git clone --depth 1 https://github.com/KhronosGroup/Vulkan-Headers /tmp/vkh
#   make layer VULKAN_INC=/tmp/vkh/include
$(LAYER_SRC): ;

# ---------------------------------------------------------------- dev
run:
	$(PY) server/streamframe.py -v

dev:                      # page from this checkout, picture and /status from UPSTREAM
	$(PY) scripts/devserver.py --upstream $(UPSTREAM)

test:
	$(PY) -m pytest -q tests

lint:
	$(PY) -m ruff check server tests || true
	$(PY) -m py_compile server/streamframe.py server/settings.py server/gaze.py server/hotspot.py server/clip.py

# ---------------------------------------------------------------- install
install: install-layer install-units
	@echo "done — enable with: systemctl --user enable --now streamframe.service"

install-layer: layer
	install -d $(PREFIX)/layer $(PREFIX)/bin
	install -m 755 $(LAYER_OUT) $(PREFIX)/layer/libvrcap.so
	install -m 644 $(MANIFEST) $(PREFIX)/layer/vrcap_layer.json
	@echo "layer installed to $(PREFIX)/layer — see docs/install.md to register it"

install-units:
	install -d $(PREFIX)/bin $(PREFIX)/config $(PREFIX)/server/web $(PREFIX)/systemd
	install -m 755 server/streamframe.py $(PREFIX)/server/streamframe.py
	install -m 644 server/settings.py $(PREFIX)/server/settings.py
	install -m 644 server/gaze.py $(PREFIX)/server/gaze.py
	install -m 644 server/hotspot.py $(PREFIX)/server/hotspot.py
	install -m 644 server/clip.py $(PREFIX)/server/clip.py
	install -m 644 server/vrstreamd.py $(PREFIX)/server/vrstreamd.py   # alias for existing units
	install -m 644 server/web/index.html $(PREFIX)/server/web/index.html
	install -m 644 server/web/qr.js $(PREFIX)/server/web/qr.js
	install -m 644 config/mediamtx.yml $(PREFIX)/config/mediamtx.yml
	install -m 644 systemd/streamframe.service $(PREFIX)/systemd/
	install -m 644 systemd/mediamtx.service $(PREFIX)/systemd/
	install -d $(HOME)/.config/systemd/user
	install -m 644 systemd/streamframe.service $(HOME)/.config/systemd/user/
	install -m 644 systemd/mediamtx.service $(HOME)/.config/systemd/user/
	systemctl --user daemon-reload
	@echo "units installed — download mediamtx into $(PREFIX)/bin first (scripts/install.sh does it)"

clean:
	rm -rf build
