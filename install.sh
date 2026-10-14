#!/usr/bin/env bash
# Markdown Web Browser - All-in-One Installer Script
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Dicklesworthstone/markdown_web_browser/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/Dicklesworthstone/markdown_web_browser/main/install.sh | bash -s -- --yes
#   wget -qO- https://raw.githubusercontent.com/Dicklesworthstone/markdown_web_browser/main/install.sh | bash
#
# Options:
#   --yes, -y, --easy-mode Skip all confirmations (non-interactive mode)
#   --dir=PATH            Installation directory (default: ./markdown_web_browser)
#   --no-deps             Skip system dependency installation
#   --no-browsers         Skip Playwright browser installation
#   --ocr-key=KEY         Set OCR API key directly
#   --help, -h            Show this help message

set -euo pipefail

# Configuration
REPO_URL="${MDWB_REPO_URL:-https://github.com/Dicklesworthstone/markdown_web_browser.git}"
DEFAULT_INSTALL_DIR="./markdown_web_browser"
PYTHON_VERSION="${MDWB_PYTHON_VERSION:-3.13}"
APT_LOCK_WAIT_SECONDS="${MDWB_APT_LOCK_WAIT_SECONDS:-300}"
# Verified official versioned installer bytes; changes require a fresh hash review.
readonly UV_INSTALLER_VERSION="0.13.0"
readonly UV_INSTALLER_SHA256="283cbef4bdaca819bd896b0dacb8a085c4c05dbc880642aa5001d97bc4db74ba"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default options
INSTALL_DIR="$DEFAULT_INSTALL_DIR"
# Auto-detect CI/non-interactive environments
if [ "${CI:-}" = "true" ] || [ "${NONINTERACTIVE:-}" = "1" ] || [ ! -t 0 ]; then
    SKIP_CONFIRM=true
else
    SKIP_CONFIRM=false
fi
INSTALL_DEPS=true
INSTALL_BROWSERS=true
OCR_API_KEY=""

# Function to print colored output
print_color() {
    local color=$1
    shift
    printf '%b%s%b\n' "$color" "$*" "$NC"
}

# Function to print usage
usage() {
    cat << EOF
Markdown Web Browser - All-in-One Installer

Usage: $0 [OPTIONS]

Options:
    --yes, -y, --easy-mode Skip all confirmations (non-interactive mode)
    --dir=PATH            Installation directory (default: $DEFAULT_INSTALL_DIR)
    --no-deps             Skip system dependency installation
    --no-browsers         Skip Playwright browser installation
    --ocr-key=KEY         Set OCR API key directly
    --help, -h            Show this help message

Examples:
    # Interactive installation
    $0

    # Non-interactive with all defaults
    $0 --yes

    # Custom directory with OCR key
    $0 --dir=/opt/mdwb --ocr-key=sk-YOUR-KEY

    # Skip browser installation (useful for headless servers)
    $0 --no-browsers

    # Automated deployment (skip all confirmations)
    $0 --yes

EOF
    exit 0
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --yes|-y|--easy-mode)
            SKIP_CONFIRM=true
            shift
            ;;
        --dir)
            if [ -z "${2:-}" ]; then
                print_color "$RED" "Error: --dir requires a path argument"
                exit 1
            fi
            INSTALL_DIR="$2"
            shift 2
            ;;
        --dir=*)
            INSTALL_DIR="${1#*=}"
            shift
            ;;
        --no-deps)
            INSTALL_DEPS=false
            shift
            ;;
        --no-browsers)
            INSTALL_BROWSERS=false
            shift
            ;;
        --ocr-key)
            if [ -z "${2:-}" ]; then
                print_color "$RED" "Error: --ocr-key requires a value"
                exit 1
            fi
            OCR_API_KEY="$2"
            shift 2
            ;;
        --ocr-key=*)
            OCR_API_KEY="${1#*=}"
            shift
            ;;
        --help|-h)
            usage
            ;;
        *)
            print_color "$RED" "Unknown option: $1"
            usage
            ;;
    esac
done

# Function to check if command exists
command_exists() {
    command -v "$1" &> /dev/null
}

sudo_noninteractive_works() {
    command_exists sudo && sudo -n true 2>/dev/null
}

apt_lock_is_held() {
    local lockfile="$1"

    [ -e "$lockfile" ] || return 1

    if command_exists fuser; then
        if fuser "$lockfile" >/dev/null 2>&1; then
            return 0
        fi

        if sudo_noninteractive_works && sudo -n fuser "$lockfile" >/dev/null 2>&1; then
            return 0
        fi
    fi

    if command_exists lsof; then
        if lsof "$lockfile" >/dev/null 2>&1; then
            return 0
        fi

        if sudo_noninteractive_works && sudo -n lsof "$lockfile" >/dev/null 2>&1; then
            return 0
        fi
    fi

    return 1
}

first_held_apt_lock() {
    local lockfile
    for lockfile in \
        /var/lib/dpkg/lock-frontend \
        /var/lib/dpkg/lock \
        /var/lib/apt/lists/lock \
        /var/cache/apt/archives/lock
    do
        if apt_lock_is_held "$lockfile"; then
            printf '%s\n' "$lockfile"
            return 0
        fi
    done

    return 1
}

wait_for_apt_locks() {
    local waited=0
    local interval=5
    local lockfile

    while lockfile="$(first_held_apt_lock)"; do
        if [ "$waited" -ge "$APT_LOCK_WAIT_SECONDS" ]; then
            print_color "$RED" "Error: apt/dpkg lock still held after ${APT_LOCK_WAIT_SECONDS}s: $lockfile"
            print_color "$YELLOW" "Wait for other package operations to finish, then rerun the installer."
            return 1
        fi

        if [ "$waited" -eq 0 ]; then
            print_color "$YELLOW" "APT is busy ($lockfile); waiting for the current package operation to finish..."
        fi

        sleep "$interval"
        waited=$((waited + interval))
    done
}

run_apt_get() {
    wait_for_apt_locks
    sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1 apt-get "$@"
}

# Function to detect OS
detect_os() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if command_exists apt-get; then
            echo "debian"
        elif command_exists yum; then
            echo "redhat"
        elif command_exists pacman; then
            echo "arch"
        else
            echo "unknown"
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macos"
    else
        echo "unknown"
    fi
}

# Function to install uv
verify_uv_installer() {
    local installer_path="$1"
    local observed
    if command_exists sha256sum; then
        observed=$(sha256sum "$installer_path") || return 1
    elif command_exists shasum; then
        observed=$(shasum -a 256 "$installer_path") || return 1
    else
        print_color "$RED" "Error: sha256sum or shasum is required to verify the uv installer."
        return 1
    fi
    observed="${observed%% *}"
    if [ "$observed" != "$UV_INSTALLER_SHA256" ]; then
        print_color "$RED" "Error: uv installer checksum mismatch; downloaded code was not executed."
        return 1
    fi
}

install_uv() {
    if command_exists uv; then
        print_color "$GREEN" "✓ uv is already installed"
        return 0
    fi

    print_color "$BLUE" "Installing uv package manager..."

    local download_root installer_path installer_url
    download_root=$(mktemp -d "${TMPDIR:-/tmp}/mdwb-uv.XXXXXXXX") || return 1
    installer_path="$download_root/install.sh"
    installer_url="https://astral.sh/uv/$UV_INSTALLER_VERSION/install.sh"
    if command_exists curl; then
        curl --user-agent "OpenAI File Downloader, XaiImageApiFetch/1.0" \
            --fail --location --proto '=https' "$installer_url" --output "$installer_path" || return 1
    elif command_exists wget; then
        wget --user-agent="OpenAI File Downloader, XaiImageApiFetch/1.0" \
            --https-only "$installer_url" --output-document="$installer_path" || return 1
    else
        print_color "$RED" "Error: Neither curl nor wget found. Please install one of them first."
        exit 1
    fi
    verify_uv_installer "$installer_path" || return 1
    print_color "$BLUE" "Verified uv $UV_INSTALLER_VERSION installer; retained at $installer_path"
    UV_NO_MODIFY_PATH=1 sh "$installer_path" || return 1

    # Add uv to PATH for current session
    # uv 0.10+ installs to ~/.local/bin; older versions used ~/.cargo/bin
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

    if command_exists uv; then
        print_color "$GREEN" "✓ uv installed successfully"
    else
        print_color "$RED" "Error: uv installation failed"
        exit 1
    fi
}

# Function to install system dependencies
install_system_deps() {
    if [ "$INSTALL_DEPS" = false ]; then
        print_color "$YELLOW" "Skipping system dependency installation"
        return 0
    fi

    local os_type
    os_type=$(detect_os)

    print_color "$BLUE" "Installing system dependencies for $os_type..."

    case $os_type in
        debian)
            if [ "$SKIP_CONFIRM" = false ]; then
                read -p "Install system dependencies (libvips-dev, git)? [Y/n] " -n 1 -r
                echo
                if [[ ! $REPLY =~ ^[Yy]$ ]] && [ -n "$REPLY" ]; then
                    print_color "$YELLOW" "Skipping system dependencies"
                    return 0
                fi
            fi

            print_color "$BLUE" "Installing libvips and other dependencies..."
            run_apt_get update
            run_apt_get install -y libvips-dev git
            ;;

        macos)
            if ! command_exists brew; then
                print_color "$RED" "Homebrew is required on macOS. Please install it first:"
                print_color "$YELLOW" "/bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
                exit 1
            fi

            print_color "$BLUE" "Installing libvips via Homebrew..."
            brew install vips
            ;;

        redhat)
            print_color "$BLUE" "Installing libvips on RedHat-based system..."
            sudo yum install -y vips-devel git
            ;;

        arch)
            print_color "$BLUE" "Installing libvips on Arch Linux..."
            sudo pacman -S --noconfirm libvips git
            ;;

        *)
            print_color "$YELLOW" "Unknown OS. Please install libvips manually:"
            print_color "$YELLOW" "  Ubuntu/Debian: sudo apt-get install libvips-dev"
            print_color "$YELLOW" "  macOS: brew install vips"
            print_color "$YELLOW" "  RedHat/CentOS: sudo yum install vips-devel"
            print_color "$YELLOW" "  Arch Linux: sudo pacman -S libvips"

            if [ "$SKIP_CONFIRM" = false ]; then
                read -p "Continue anyway? [y/N] " -n 1 -r
                echo
                if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                    exit 1
                fi
            fi
            ;;
    esac

    print_color "$GREEN" "✓ System dependencies installed"
}

# Function to clone or update repository
setup_repository() {
    # Validate INSTALL_DIR before any dangerous operations
    if [ -z "$INSTALL_DIR" ] || [ "$INSTALL_DIR" = "/" ] || [ "$INSTALL_DIR" = "." ] || [ "$INSTALL_DIR" = ".." ] || [ "$INSTALL_DIR" = "/usr" ] || [ "$INSTALL_DIR" = "/bin" ] || [ "$INSTALL_DIR" = "/etc" ] || [ "$INSTALL_DIR" = "/home" ] || [ "$INSTALL_DIR" = "$HOME" ]; then
        print_color "$RED" "Error: Invalid or dangerous installation directory: '$INSTALL_DIR'"
        exit 1
    fi

    # Check if git is available
    if ! command_exists git; then
        print_color "$RED" "Error: git is not installed."
        print_color "$YELLOW" "Please install git or run without --no-deps flag."
        exit 1
    fi

    if [ -d "$INSTALL_DIR/.git" ]; then
        print_color "$BLUE" "Updating existing repository..."
        cd "$INSTALL_DIR" || return 1
        git -c http.userAgent="OpenAI File Downloader, XaiImageApiFetch/1.0" pull --ff-only origin main
    else
        if [ -d "$INSTALL_DIR" ]; then
            print_color "$RED" "Error: $INSTALL_DIR exists but is not a git checkout."
            print_color "$YELLOW" "Choose a different --dir path or move the existing directory aside before reinstalling."
            exit 1
        fi

        print_color "$BLUE" "Cloning repository..."
        git -c http.userAgent="OpenAI File Downloader, XaiImageApiFetch/1.0" clone "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR" || return 1
    fi

    print_color "$GREEN" "✓ Repository ready"
}

# Function to setup Python environment
setup_python_env() {
    print_color "$BLUE" "Setting up Python environment..."

    # Install Python if needed
    uv python install "$PYTHON_VERSION"

    # Create virtual environment
    if [ ! -d ".venv" ]; then
        uv venv --python "$PYTHON_VERSION"
    fi

    # Sync dependencies
    print_color "$BLUE" "Installing Python dependencies..."
    uv sync --locked --python "$PYTHON_VERSION"

    print_color "$GREEN" "✓ Python environment ready"
}

# Print guidance for installing browser system dependencies manually.
# Used when Playwright's automatic `--with-deps` step is unavailable for the
# host distro (e.g. a Playwright release that does not yet recognize a brand
# new Ubuntu version). This is advisory only and never aborts the install.
print_browser_dep_notice() {
    local os_type
    os_type=$(detect_os)

    print_color "$YELLOW" "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    print_color "$YELLOW" "⚠  System browser dependencies were NOT installed automatically."
    print_color "$YELLOW" "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    case "$os_type" in
        debian)
            print_color "$YELLOW" "  To run the bundled Chromium, install its runtime libraries manually:"
            print_color "$YELLOW" "    sudo apt-get install -y \\"
            print_color "$YELLOW" "      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \\"
            print_color "$YELLOW" "      libdrm2 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \\"
            print_color "$YELLOW" "      libxrandr2 libgbm1 libxkbcommon0 libasound2 libpango-1.0-0 libcairo2"
            print_color "$YELLOW" "  (Some package names vary by release, e.g. libasound2 -> libasound2t64.)"
            ;;
        redhat)
            print_color "$YELLOW" "  Install Chromium runtime libraries with: sudo yum install -y \\"
            print_color "$YELLOW" "    nss nspr atk at-spi2-atk cups-libs libdrm libXcomposite \\"
            print_color "$YELLOW" "    libXdamage libXfixes libXrandr mesa-libgbm libxkbcommon alsa-lib"
            ;;
        arch)
            print_color "$YELLOW" "  Install Chromium runtime libraries with: sudo pacman -S \\"
            print_color "$YELLOW" "    nss nspr atk at-spi2-atk cups libdrm libxcomposite libxdamage \\"
            print_color "$YELLOW" "    libxfixes libxrandr mesa libxkbcommon alsa-lib"
            ;;
        macos)
            print_color "$YELLOW" "  No extra system packages are typically required on macOS."
            ;;
        *)
            print_color "$YELLOW" "  Install the Chromium runtime libraries for your distribution manually."
            ;;
    esac
    print_color "$YELLOW" ""
    print_color "$YELLOW" "  The default capture channel requires the exact Chrome for Testing build"
    print_color "$YELLOW" "  named by CFT_VERSION. Install that build from https://googlechromelabs.github.io/chrome-for-testing/"
    print_color "$YELLOW" "  and set CFT_EXECUTABLE to its Chrome executable, or use the documented"
    print_color "$YELLOW" "  ~/.cache/mdwb/cft/<build>/chrome-linux64/chrome location on Linux."
    print_color "$YELLOW" "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# Function to install Playwright browsers
install_playwright_browsers() {
    if [ "$INSTALL_BROWSERS" = false ]; then
        print_color "$YELLOW" "Skipping Playwright browser installation"
        return 0
    fi

    print_color "$BLUE" "Installing Playwright Chromium..."
    print_color "$YELLOW" "  This installs Playwright's bundled Chromium for explicitly selected chromium runs."
    print_color "$YELLOW" "  It does not install the exact Chrome for Testing build named by CFT_VERSION."

    # Playwright's bundled Chromium and Chrome for Testing are distinct artifacts.
    if [ "$(detect_os)" = "debian" ]; then
        wait_for_apt_locks
    fi

    # Try the full install (browser + apt system deps) first. On distro releases
    # that a given Playwright version does not yet recognize (e.g. Ubuntu 26.04
    # "resolute"), `--with-deps` aborts with errors such as:
    #     Cannot install dependencies for ubuntu26.04-x64
    #     Playwright does not support chromium on ubuntu26.04-x64
    # We detect that by the command's own exit status (no distro version is
    # hard-coded) and fall back to a browser-only install so the tool still
    # works against the bundled Chromium or system Chrome. This whole step is
    # best-effort and must never abort the overall installation.
    if uv run playwright install chromium --with-deps; then
        print_color "$GREEN" "✓ Playwright Chromium installed (with system dependencies)"
    else
        print_color "$YELLOW" "⚠ 'playwright install chromium --with-deps' failed on this system."
        print_color "$YELLOW" "  This is expected when Playwright does not yet support this distro"
        print_color "$YELLOW" "  release (common on brand-new Ubuntu versions). Falling back to a"
        print_color "$YELLOW" "  browser-only install without automatic system-dependency resolution..."

        if uv run playwright install chromium; then
            print_color "$GREEN" "✓ Playwright Chromium browser installed (without system deps)"
            print_browser_dep_notice
        else
            print_color "$YELLOW" "⚠ Playwright could not install its bundled Chromium on this host."
            print_color "$YELLOW" "  Browser installation remains incomplete. See guidance below."
            print_browser_dep_notice
        fi
    fi

    print_color "$YELLOW" "  Before capture, install the exact CfT build from:"
    print_color "$YELLOW" "    https://googlechromelabs.github.io/chrome-for-testing/"
    print_color "$YELLOW" "  Set CFT_EXECUTABLE to that executable; capture verifies its observed build."

    return 0
}

# Function to setup configuration
setup_config() {
    print_color "$BLUE" "Setting up configuration..."

    if [ -e ".env" ] || [ -L ".env" ]; then
        print_color "$GREEN" "✓ Preserved existing .env without modification"
        if [ -n "$OCR_API_KEY" ]; then
            print_color "$YELLOW" "  --ocr-key was not applied because .env already exists."
        fi
        return 0
    fi
    if [ ! -f ".env.example" ]; then
        print_color "$RED" "Error: .env.example is missing; configuration was not created."
        return 1
    fi
    if [[ "$OCR_API_KEY" == *$'\n'* || "$OCR_API_KEY" == *$'\r'* ]]; then
        print_color "$RED" "Error: OCR API key must be a single line."
        return 1
    fi
    # Exclusive creation also refuses symlinks and files created after the check.
    if ! (
        umask 077
        MDWB_INSTALL_OCR_API_KEY="$OCR_API_KEY" uv run --python 3.13 python - <<'PY'
from pathlib import Path
from app.settings import load_config

key = load_config(".env.example")("MDWB_INSTALL_OCR_API_KEY", default="")
template = Path(".env.example").read_text(encoding="utf-8")
lines = template.splitlines()
if key:
    lines = [f"OLMOCR_API_KEY={key}" if line.startswith("OLMOCR_API_KEY=") else line for line in lines]
with Path(".env").open("x", encoding="utf-8") as stream:
    stream.write("\n".join(lines) + "\n")
PY
    ); then
        print_color "$RED" "Error: .env could not be created exclusively; existing configuration was preserved."
        return 1
    fi
    print_color "$GREEN" "✓ Created private .env from .env.example"
    print_color "$YELLOW" "  Review OCR credentials and the exact CFT_VERSION/CFT_EXECUTABLE before capture."
}

# Function to run tests
run_tests() {
    print_color "$BLUE" "Running verification tests..."

    local all_passed=true

    # Test pyvips import
    if uv run --python 3.13 python -c "import pyvips; print('✓ pyvips works')"; then
        print_color "$GREEN" "✓ pyvips import successful"
    else
        print_color "$RED" "✗ pyvips import failed - libvips may not be installed correctly"
        all_passed=false
    fi

    # Test Playwright
    if [ "$INSTALL_BROWSERS" = true ]; then
        if uv run --python 3.13 python -c "from playwright.async_api import async_playwright; print('✓ Playwright works')"; then
            print_color "$GREEN" "✓ Playwright import successful"
        else
            print_color "$RED" "✗ Playwright import failed"
            all_passed=false
        fi

        if uv run --python 3.13 python - <<'PY'
import asyncio
from playwright.async_api import async_playwright
from app.capture import _launch_browser
from app.settings import get_settings

async def probe():
    settings = get_settings().browser
    async with async_playwright() as playwright:
        browser, channel, transport = await _launch_browser(
            playwright, settings.playwright_channel,
            expected_build=settings.cft_version, transport=settings.browser_transport,
            executable=settings.cft_executable, fontconfig_file=settings.fontconfig_file,
        )
        try:
            print(f"Observed capture browser: {channel} {browser.version} {transport}")
        finally:
            await browser.close()

asyncio.run(probe())
PY
        then
            print_color "$GREEN" "✓ Configured browser launch, observed build, and glyph preflight passed"
        else
            print_color "$RED" "✗ Configured capture browser preflight failed"
            print_color "$YELLOW" "  Install the exact CfT build and set CFT_EXECUTABLE; see the error above."
            all_passed=false
        fi
    fi

    # Test CLI
    if uv run --python 3.13 python -m scripts.mdwb_cli --help > /dev/null; then
        print_color "$GREEN" "✓ CLI tool works"
    else
        print_color "$RED" "✗ CLI tool failed"
        all_passed=false
    fi

    # Test environment configuration
    if uv run --python 3.13 python - <<'PY'
from app.settings import get_settings
ocr = get_settings().ocr
if not ocr.local_url and (not ocr.api_key or ocr.api_key == "sk-***"):
    raise SystemExit("Configure a local OCR URL or a hosted OCR API key before capture.")
print("OCR configuration is present; provider admission and output quality remain unverified.")
PY
    then
        print_color "$GREEN" "✓ OCR configuration is present"
    else
        all_passed=false
    fi

    if [ "$all_passed" = true ]; then
        return 0
    else
        return 1
    fi
}

# Function to create launcher script
create_launcher() {
    # We're already in INSTALL_DIR, so just use relative path
    local launcher_path="./mdwb"

    cat > "$launcher_path" << 'EOF'
#!/usr/bin/env bash
# Markdown Web Browser CLI Launcher

# Resolve the real location of this script, following symlinks, before locating
# the bundled `scripts/` package. Deriving SCRIPT_DIR straight from
# ${BASH_SOURCE[0]} would point at a symlink's own directory when the launcher
# is invoked through one (e.g. ~/.local/bin/mdwb), and the relative
# `-m scripts.mdwb_cli` import would fail with "No module named 'scripts'".
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    dir="$( cd -P "$( dirname "$SOURCE" )" >/dev/null 2>&1 && pwd )"
    SOURCE="$( readlink "$SOURCE" )"
    [[ $SOURCE != /* ]] && SOURCE="$dir/$SOURCE"
done
SCRIPT_DIR="$( cd -P "$( dirname "$SOURCE" )" >/dev/null 2>&1 && pwd )"
cd "$SCRIPT_DIR"

# Ensure we're using the virtual environment
exec uv run python -m scripts.mdwb_cli "$@"
EOF

    chmod +x "$launcher_path"

    # Get absolute path for display
    local abs_launcher_path
    abs_launcher_path="$(cd "$(dirname "$launcher_path")" && pwd)/$(basename "$launcher_path")"

    print_color "$GREEN" "✓ Created launcher script: $abs_launcher_path"

    # Expose `mdwb` on PATH so it is invocable by name and discoverable via
    # `command -v mdwb` (doctors such as ntm/acfs rely on this).
    install_path_wrapper "$abs_launcher_path"
}

# Function to install a PATH wrapper for the launcher and ensure the bin dir is
# on PATH. We install a tiny wrapper (not a bare symlink) that execs the real
# launcher by ABSOLUTE path: that keeps the launcher's own directory resolution
# correct regardless of where the wrapper lives, and works even on shells/OSes
# where symlink resolution is quirky.
install_path_wrapper() {
    local real_launcher="$1"
    local bin_dir="${MDWB_BIN_DIR:-$HOME/.local/bin}"
    local wrapper="$bin_dir/mdwb"

    if ! mkdir -p "$bin_dir" 2>/dev/null; then
        print_color "$YELLOW" "⚠ Could not create $bin_dir; 'mdwb' not placed on PATH."
        print_color "$YELLOW" "  Add an alias instead: alias mdwb='$real_launcher'"
        return 0
    fi

    cat > "$wrapper" << EOF
#!/usr/bin/env bash
# Auto-generated by the markdown_web_browser installer.
# Forwards to the real launcher at its install location.
exec "$real_launcher" "\$@"
EOF
    chmod +x "$wrapper"
    print_color "$GREEN" "✓ Installed 'mdwb' on PATH: $wrapper"

    ensure_on_path "$bin_dir"
}

# Function to ensure a bin dir is on PATH. If it is not on the current PATH, add
# it to the user's shell profiles idempotently (marker-guarded) and tell the
# user how to activate it in the current session.
ensure_on_path() {
    local bin_dir="$1"

    case ":$PATH:" in
        *":$bin_dir:"*)
            return 0  # already reachable this session; wrapper is usable now
            ;;
    esac

    local marker="# added by markdown_web_browser installer"
    local line="export PATH=\"$bin_dir:\$PATH\"  $marker"
    local wrote_any=false
    local rc

    # Append to ~/.profile (login-shell fallback; created if absent) and to any
    # existing interactive rc files. Guard with a marker so re-running is a no-op.
    for rc in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc"; do
        if [ -f "$rc" ] || [ "$rc" = "$HOME/.profile" ]; then
            if grep -Fqs "$marker" "$rc" 2>/dev/null; then
                wrote_any=true
            elif printf '\n%s\n' "$line" >> "$rc" 2>/dev/null; then
                wrote_any=true
            fi
        fi
    done

    print_color "$YELLOW" "  '$bin_dir' is not currently on your PATH."
    if [ "$wrote_any" = true ]; then
        print_color "$YELLOW" "  Added it to your shell profile; open a new terminal, or run now:"
    else
        print_color "$YELLOW" "  Add it to your PATH by running:"
    fi
    print_color "$YELLOW" "    export PATH=\"$bin_dir:\$PATH\""
}

# Main installation flow
main() {
    if [ "$PYTHON_VERSION" != "3.13" ]; then
        print_color "$RED" "Error: this project requires Python 3.13."
        return 1
    fi
    print_color "$BLUE" "════════════════════════════════════════════════"
    print_color "$BLUE" "   Markdown Web Browser - All-in-One Installer  "
    print_color "$BLUE" "════════════════════════════════════════════════"
    echo

    # Show installation plan
    print_color "$YELLOW" "Installation Plan:"
    print_color "$YELLOW" "  • Install directory: $INSTALL_DIR"
    print_color "$YELLOW" "  • Python version: $PYTHON_VERSION"
    print_color "$YELLOW" "  • Install system deps: $INSTALL_DEPS"
    print_color "$YELLOW" "  • Install browsers: $INSTALL_BROWSERS"

    if [ -n "$OCR_API_KEY" ]; then
        print_color "$YELLOW" "  • OCR API key: provided (applied only when creating a new .env)"
    fi
    echo

    # Confirm installation
    if [ "$SKIP_CONFIRM" = false ]; then
        read -p "Proceed with installation? [Y/n] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]] && [ -n "$REPLY" ]; then
            print_color "$YELLOW" "Installation cancelled"
            exit 0
        fi
    fi

    # Create installation directory
    mkdir -p "$(dirname "$INSTALL_DIR")"

    # Run installation steps
    install_uv
    install_system_deps
    setup_repository
    setup_python_env
    install_playwright_browsers
    setup_config

    # Run tests
    echo
    if run_tests; then
        print_color "$GREEN" "✓ Installation preflights passed; live capture qualification remains separate"
    else
        print_color "$YELLOW" "⚠ Some tests failed - review messages above"
        return 1
    fi

    # Create launcher
    echo
    create_launcher

    # Print success message
    echo
    print_color "$GREEN" "════════════════════════════════════════════════"
    print_color "$GREEN" "   Installation Complete!                       "
    print_color "$GREEN" "════════════════════════════════════════════════"
    echo

    # Get absolute install path for display
    local abs_install_path
    abs_install_path="$(pwd)"

    print_color "$BLUE" "Quick Start:"
    print_color "$BLUE" "  cd $abs_install_path"

    print_color "$BLUE" "  # Start the API and browser UI"
    print_color "$BLUE" "  uv run --python 3.13 python scripts/run_server.py --workers 1"
    print_color "$BLUE" "  # In another terminal, submit your first capture"
    print_color "$BLUE" "  uv run --python 3.13 python -m scripts.mdwb_cli fetch https://example.com --watch"
    echo
    print_color "$BLUE" "Or use the launcher:"
    print_color "$BLUE" "  $abs_install_path/mdwb fetch https://example.com"
    echo

    if [ "$INSTALL_BROWSERS" = true ]; then
        print_color "$BLUE" "Capture Browser Information:"
        print_color "$BLUE" "  • CFT_VERSION requires an exact Chrome for Testing executable"
        print_color "$BLUE" "  • Captures record the observed browser build and transport"
        print_color "$BLUE" "  • Review CFT_EXECUTABLE, CFT_VERSION, and CFT_LABEL in your preserved configuration"
        echo
    fi

    print_color "$GREEN" "Happy browsing! 🎉"
}

# Run main function
if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
    main
fi
