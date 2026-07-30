#!/bin/sh

set -eu

readonly TRADE_COMPASS_DEFAULT_PACKAGE="trade-compass-agent==0.2.0"
readonly TRADE_COMPASS_DEFAULT_PYTHON="3.12"
readonly TRADE_COMPASS_DEFAULT_UV_VERSION="0.11.32"
readonly TRADE_COMPASS_DEFAULT_UV_INSTALLER_SHA256="43aff33a967fe40e8c17949d8c85c65bc43f3b5c94742393c957f56ab5ba80f4"

installer_tmp=""

cleanup() {
  if [ -n "$installer_tmp" ] && [ -d "$installer_tmp" ]; then
    rm -rf -- "$installer_tmp"
  fi
}

fail() {
  printf '\n安装失败：%s\n' "$1" >&2
  exit 1
}

show_help() {
  cat <<'EOF'
Trade Compass Agent 安装器

用法：
  sh install.sh

可选环境变量：
  TRADE_COMPASS_PACKAGE       安装目标，默认当前 Release 的精确版本
  TRADE_COMPASS_PYTHON        Python 版本，默认 3.12
  TRADE_COMPASS_UV_VERSION    引导安装的 uv 版本，默认 0.11.32
  TRADE_COMPASS_UV_INSTALLER_URL
                              自定义 uv 官方安装器地址
  TRADE_COMPASS_UV_INSTALLER_SHA256
                              自定义安装器地址或版本时必须提供

安装完成后不会自动运行 trade-compass setup。
EOF
}

verify_sha256() {
  expected="$1"
  target="$2"
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$target")"
  elif command -v shasum >/dev/null 2>&1; then
    actual="$(shasum -a 256 "$target")"
  else
    fail "系统缺少 sha256sum 或 shasum，无法校验 uv 安装器。"
  fi
  actual="${actual%% *}"
  [ "$actual" = "$expected" ] || fail "uv 安装器 SHA-256 校验失败。"
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  show_help
  exit 0
fi
if [ "$#" -ne 0 ]; then
  fail "不支持的参数：$1（使用 --help 查看用法）"
fi

case "$(uname -s)" in
  Darwin | Linux) ;;
  *) fail "目前仅支持 macOS 和 Linux。" ;;
esac

package="${TRADE_COMPASS_PACKAGE:-$TRADE_COMPASS_DEFAULT_PACKAGE}"
python_version="${TRADE_COMPASS_PYTHON:-$TRADE_COMPASS_DEFAULT_PYTHON}"
uv_version="${TRADE_COMPASS_UV_VERSION:-$TRADE_COMPASS_DEFAULT_UV_VERSION}"
uv_installer_url="${TRADE_COMPASS_UV_INSTALLER_URL:-https://astral.sh/uv/${uv_version}/install.sh}"
uv_installer_sha256="${TRADE_COMPASS_UV_INSTALLER_SHA256:-}"

printf '\n正在安装 Trade Compass Agent…\n'

uv_bin="$(command -v uv || true)"
if [ -z "$uv_bin" ]; then
  [ -n "${HOME:-}" ] || fail "未设置 HOME，无法安全安装 uv。"
  command -v mktemp >/dev/null 2>&1 || fail "系统缺少 mktemp。"

  installer_tmp="$(mktemp -d)"
  trap cleanup EXIT
  trap 'exit 1' HUP INT TERM
  uv_installer="$installer_tmp/uv-install.sh"

  if command -v curl >/dev/null 2>&1; then
    curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
      --output "$uv_installer" \
      "$uv_installer_url"
  elif command -v wget >/dev/null 2>&1; then
    wget --https-only --quiet --output-document="$uv_installer" "$uv_installer_url"
  else
    fail "需要 curl 或 wget 才能安装 uv。"
  fi

  if [ -z "$uv_installer_sha256" ]; then
    if [ "$uv_version" = "$TRADE_COMPASS_DEFAULT_UV_VERSION" ] &&
      [ "$uv_installer_url" = "https://astral.sh/uv/${TRADE_COMPASS_DEFAULT_UV_VERSION}/install.sh" ]; then
      uv_installer_sha256="$TRADE_COMPASS_DEFAULT_UV_INSTALLER_SHA256"
    else
      fail "自定义 uv 安装器地址或版本时必须设置 TRADE_COMPASS_UV_INSTALLER_SHA256。"
    fi
  fi
  verify_sha256 "$uv_installer_sha256" "$uv_installer"

  uv_install_dir="${UV_INSTALL_DIR:-$HOME/.local/bin}"
  UV_INSTALL_DIR="$uv_install_dir"
  UV_NO_MODIFY_PATH=1
  export UV_INSTALL_DIR UV_NO_MODIFY_PATH
  sh "$uv_installer"

  uv_bin="$uv_install_dir/uv"
  [ -x "$uv_bin" ] || fail "uv 安装完成，但未在 $uv_install_dir 找到可执行文件。"
fi

"$uv_bin" tool install --upgrade --python "$python_version" "$package"

tool_bin_dir="$("$uv_bin" tool dir --bin)"
trade_compass_bin="$tool_bin_dir/trade-compass"
[ -x "$trade_compass_bin" ] || fail "安装完成，但未在 $tool_bin_dir 找到 trade-compass。"

installed_version="$("$trade_compass_bin" --version)"

printf '\n'
printf '╭──────────────────────────────────────────────╮\n'
printf '│  欢迎使用 Trade Compass Agent（交易罗盘）   │\n'
printf '╰──────────────────────────────────────────────╯\n'
printf '\n安装成功：%s\n' "$installed_version"

case ":${PATH:-}:" in
  *":$tool_bin_dir:"*) ;;
  *)
    printf '\n当前终端还需要把命令目录加入 PATH：\n'
    printf '  export PATH="%s:$PATH"\n' "$tool_bin_dir"
    ;;
esac

printf '\n接下来可以按顺序执行：\n'
printf '  1. trade-compass setup         完成首次配置\n'
printf '  2. trade-compass doctor        检查运行环境\n'
printf '  3. trade-compass serve --open  启动 Web 工作台\n'
printf '\n安装器不会自动启动配置向导，所有配置由你决定何时开始。\n\n'
