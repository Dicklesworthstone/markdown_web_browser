#!/usr/bin/env bash
# Markdown Web Browser - All-in-One Installer Script
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/anthropics/markdown_web_browser/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/anthropics/markdown_web_browser/main/install.sh | bash -s -- --yes
#   wget -qO- https://raw.githubusercontent.com/anthropics/markdown_web_browser/main/install.sh | bash
#
# Options:
#   --yes, -y              Skip all confirmations (non-interactive mode)
#   --dir=PATH            Installation directory (default: ./markdown_web_browser)
#   --no-deps             Skip system dependency installation
#   --no-browsers         Skip Playwright browser installation
#   --ocr-key=KEY         Set OCR API key directly
#   --help, -h            Show this help message

set -euo pipefail

# Configuration
REPO_URL="${MDWB_REPO_URL:-https://github.com/anthropics/markdown_web_browser.git}"
DEFAULT_INSTALL_DIR="./markdown_web_browser"
PYTHON_VERSION="${MDWB_PYTHON_VERSION:-3.13}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default options
INSTALL_DIR="$DEFAULT_INSTALL_DIR"
SKIP_CONFIRM=false
INSTALL_DEPS=true
INSTALL_BROWSERS=true
OCR_API_KEY=""

# Function to print colored output
print_color() {
    local color=$1
    shift
    echo -e "${color}$@${NC}"
}

# Function to print usage
usage() {
    cat << EOF
Markdown Web Browser - All-in-One Installer

Usage: $0 [OPTIONS]

Options:
    --yes, -y              Skip all confirmations (non-interactive mode)
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
        --yes|-y)
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
install_uv() {
    if command_exists uv; then
        print_color "$GREEN" "✓ uv is already installed"
        return 0
    fi

    print_color "$BLUE" "Installing uv package manager..."

    if command_exists curl; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command_exists wget; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        print_color "$RED" "Error: Neither curl nor wget found. Please install one of them first."
        exit 1
    fi

    # Add uv to PATH for current session
    export PATH="$HOME/.cargo/bin:$PATH"

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

    local os_type=$(detect_os)

    print_color "$BLUE" "Installing system dependencies for $os_type..."

    case $os_type in
        debian)
            if [ "$SKIP_CONFIRM" = false ]; then
                read -p "Install system dependencies (libvips-dev, git)? [Y/n] " -n 1 -r
                echo
                if [[ ! $REPLY =~ ^[Yy]$ ]] && [ ! -z "$REPLY" ]; then
                    print_color "$YELLOW" "Skipping system dependencies"
                    return 0
                fi
            fi

            print_color "$BLUE" "Installing libvips and other dependencies..."
            sudo apt-get update
            sudo apt-get install -y libvips-dev git
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
        cd "$INSTALL_DIR"
        git pull origin main
    else
        if [ -d "$INSTALL_DIR" ]; then
            if [ "$SKIP_CONFIRM" = false ]; then
                read -p "Directory $INSTALL_DIR exists. Remove and reinstall? [y/N] " -n 1 -r
                echo
                if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                    print_color "$YELLOW" "Installation cancelled"
                    exit 1
                fi
            fi
            # Safe to remove after validation above
            rm -rf "$INSTALL_DIR"
        fi

        print_color "$BLUE" "Cloning repository..."
        git clone "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
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
    uv sync

    print_color "$GREEN" "✓ Python environment ready"
}

# Function to detect Chrome for Testing version
detect_cft_version() {
    print_color "$BLUE" "Detecting Chrome for Testing version..."

    # Try to get version info from playwright
    local version_output=$(uv run playwright install chromium --dry-run --channel=cft 2>&1 || true)

    # Extract version from output (format varies, try multiple patterns)
    local cft_version=""

    # Try to extract version like "130.0.6723.69"
    if echo "$version_output" | grep -qE "chrome-[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+"; then
        cft_version=$(echo "$version_output" | grep -oE "chrome-[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+" | head -1)
    elif echo "$version_output" | grep -qE "[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+"; then
        # Sometimes it's just the version without "chrome-" prefix
        local raw_version=$(echo "$version_output" | grep -oE "[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+" | head -1)
        cft_version="chrome-${raw_version}"
    fi

    if [ -z "$cft_version" ]; then
        # Fallback: use a reasonable default if detection fails
        print_color "$YELLOW" "Could not auto-detect CfT version, using default"
        cft_version="chrome-130.0.6723.69"
    else
        print_color "$GREEN" "✓ Detected Chrome for Testing: $cft_version"
    fi

    echo "$cft_version"
}

# Function to install Playwright browsers
install_playwright_browsers() {
    if [ "$INSTALL_BROWSERS" = false ]; then
        print_color "$YELLOW" "Skipping Playwright browser installation"
        return 0
    fi

    print_color "$BLUE" "Installing Chrome for Testing via Playwright..."
    print_color "$YELLOW" "  Note: This installs the deterministic Chrome for Testing build,"
    print_color "$YELLOW" "        NOT regular Chromium, to ensure reproducible screenshots."

    # CRITICAL FIX: Install Chrome for Testing with --channel=cft
    uv run playwright install chromium --with-deps --channel=cft

    # Verify CfT installation with fallback checks
    local verify_output=$(uv run playwright install chromium --dry-run --channel=cft 2>&1)
    if echo "$verify_output" | grep -qE "(is already installed|already exists)"; then
        print_color "$GREEN" "✓ Chrome for Testing installed successfully"
    elif [ -d "$HOME/.cache/ms-playwright" ] || [ -d "$HOME/Library/Caches/ms-playwright" ]; then
        print_color "$YELLOW" "⚠ CfT verification uncertain, but browser cache exists - proceeding"
    else
        print_color "$RED" "✗ Chrome for Testing installation may have failed"
        print_color "$YELLOW" "  The system may not work correctly without CfT"
        return 1
    fi

    return 0
}

# Function to setup configuration
setup_config() {
    print_color "$BLUE" "Setting up configuration..."

    # Copy example env file if it doesn't exist
    if [ ! -f ".env" ]; then
        if [ -f ".env.example" ]; then
            cp .env.example .env
            print_color "$GREEN" "✓ Created .env from .env.example"
        else
            print_color "$YELLOW" "Warning: No .env.example found"
        fi
    else
        print_color "$GREEN" "✓ .env file already exists"
    fi

    # Detect and update CfT version in .env
    if [ "$INSTALL_BROWSERS" = true ] && [ -f ".env" ]; then
        local detected_version=$(detect_cft_version)

        if [ ! -z "$detected_version" ]; then
            # Update CFT_VERSION in .env (safe approach: remove old line, append new)
            if grep -q "^CFT_VERSION=" .env; then
                # Create temp file without CFT_VERSION line, then append new value
                grep -v "^CFT_VERSION=" .env > .env.tmp || true
                mv .env.tmp .env
                echo "CFT_VERSION=$detected_version" >> .env
                print_color "$GREEN" "✓ Updated CFT_VERSION in .env to $detected_version"
            else
                echo "CFT_VERSION=$detected_version" >> .env
                print_color "$GREEN" "✓ Added CFT_VERSION to .env: $detected_version"
            fi

            # Note: CFT_LABEL should be manually set based on Chrome release channel
            # Default to "Stable" if not already set
            if ! grep -q "^CFT_LABEL=" .env; then
                echo "CFT_LABEL=Stable" >> .env
                print_color "$YELLOW" "  Set CFT_LABEL=Stable (update if using specific label like Stable-1)"
            fi
        fi
    fi

    # Set OCR API key if provided
    if [ ! -z "$OCR_API_KEY" ]; then
        if [ -f ".env" ] && grep -q "^OLMOCR_API_KEY=" .env; then
            # Safe approach: remove old line, append new (handles special chars in API key)
            grep -v "^OLMOCR_API_KEY=" .env > .env.tmp || true
            mv .env.tmp .env
            echo "OLMOCR_API_KEY=$OCR_API_KEY" >> .env
            print_color "$GREEN" "✓ OCR API key configured"
        else
            echo "OLMOCR_API_KEY=$OCR_API_KEY" >> .env
            print_color "$GREEN" "✓ OCR API key added to .env"
        fi
    else
        print_color "$YELLOW" "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        print_color "$YELLOW" "⚠  IMPORTANT: OCR API key not configured"
        print_color "$YELLOW" "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        print_color "$YELLOW" "  The system REQUIRES an olmOCR API key to function."
        print_color "$YELLOW" "  Add it to .env before running:"
        print_color "$YELLOW" "    OLMOCR_API_KEY=sk-your-api-key-here"
        print_color "$YELLOW" "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    fi
}

# Function to run tests
run_tests() {
    print_color "$BLUE" "Running verification tests..."

    local all_passed=true

    # Test pyvips import
    if uv run python -c "import pyvips; print('✓ pyvips works')" 2>/dev/null; then
        print_color "$GREEN" "✓ pyvips import successful"
    else
        print_color "$RED" "✗ pyvips import failed - libvips may not be installed correctly"
        all_passed=false
    fi

    # Test Playwright
    if [ "$INSTALL_BROWSERS" = true ]; then
        if uv run python -c "from playwright.async_api import async_playwright; print('✓ Playwright works')" 2>/dev/null; then
            print_color "$GREEN" "✓ Playwright import successful"
        else
            print_color "$RED" "✗ Playwright import failed"
            all_passed=false
        fi

        # Verify Chrome for Testing is actually installed
        local cft_check=$(uv run playwright install chromium --dry-run --channel=cft 2>&1)
        if echo "$cft_check" | grep -qE "(is already installed|already exists)"; then
            print_color "$GREEN" "✓ Chrome for Testing is installed and ready"
        elif [ -d "$HOME/.cache/ms-playwright" ] || [ -d "$HOME/Library/Caches/ms-playwright" ]; then
            # Fallback: check if Playwright browser cache exists
            print_color "$YELLOW" "⚠ Chrome for Testing verification uncertain, but browser cache exists"
        else
            print_color "$RED" "✗ Chrome for Testing not detected"
            print_color "$YELLOW" "  System may not work correctly - screenshots won't be deterministic"
            all_passed=false
        fi
    fi

    # Test CLI
    if uv run python -m scripts.mdwb_cli --help > /dev/null 2>&1; then
        print_color "$GREEN" "✓ CLI tool works"
    else
        print_color "$RED" "✗ CLI tool failed"
        all_passed=false
    fi

    # Test environment configuration
    if [ -f ".env" ]; then
        # Check for non-empty OLMOCR_API_KEY value (any format)
        local api_key_value=$(grep "^OLMOCR_API_KEY=" .env 2>/dev/null | cut -d= -f2-)
        if [ ! -z "$api_key_value" ] && [ "$api_key_value" != "sk-***" ]; then
            print_color "$GREEN" "✓ OCR API key configured in .env"
        else
            print_color "$YELLOW" "⚠ OCR API key not set - system will not work until configured"
        fi
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

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Ensure we're using the virtual environment
exec uv run python -m scripts.mdwb_cli "$@"
EOF

    chmod +x "$launcher_path"

    # Get absolute path for display
    local abs_launcher_path="$(cd "$(dirname "$launcher_path")" && pwd)/$(basename "$launcher_path")"

    print_color "$GREEN" "✓ Created launcher script: $abs_launcher_path"
    print_color "$YELLOW" "  You can add it to your PATH or create an alias:"
    print_color "$YELLOW" "  alias mdwb='$abs_launcher_path'"
}

# Main installation flow
main() {
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

    if [ ! -z "$OCR_API_KEY" ]; then
        print_color "$YELLOW" "  • OCR API key: ${OCR_API_KEY:0:10}..."
    fi
    echo

    # Confirm installation
    if [ "$SKIP_CONFIRM" = false ]; then
        read -p "Proceed with installation? [Y/n] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]] && [ ! -z "$REPLY" ]; then
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
        print_color "$GREEN" "✓ All tests passed"
    else
        print_color "$YELLOW" "⚠ Some tests failed - review messages above"
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
    local abs_install_path="$(pwd)"

    print_color "$BLUE" "Quick Start:"
    print_color "$BLUE" "  cd $abs_install_path"

    if [ -z "$OCR_API_KEY" ]; then
        print_color "$YELLOW" "  # FIRST: Add your OCR API key to .env"
        print_color "$YELLOW" "  nano .env  # (set OLMOCR_API_KEY=sk-...)"
        print_color "$YELLOW" ""
        print_color "$BLUE" "  # THEN: Run your first capture"
    fi

    print_color "$BLUE" "  uv run python -m scripts.mdwb_cli fetch https://example.com"
    echo
    print_color "$BLUE" "Or use the launcher:"
    print_color "$BLUE" "  $abs_install_path/mdwb fetch https://example.com"
    echo

    if [ "$INSTALL_BROWSERS" = true ]; then
        print_color "$BLUE" "Chrome for Testing Information:"
        print_color "$BLUE" "  • Ensures deterministic, reproducible screenshots"
        print_color "$BLUE" "  • Version recorded in every manifest.json"
        print_color "$BLUE" "  • Check your .env for CFT_VERSION and CFT_LABEL settings"
        echo
    fi

    if [ -z "$OCR_API_KEY" ]; then
        print_color "$RED" "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        print_color "$RED" "  ACTION REQUIRED: Set your OCR API key in .env"
        print_color "$RED" "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        print_color "$YELLOW" "  Edit: $abs_install_path/.env"
        print_color "$YELLOW" "  Set: OLMOCR_API_KEY=sk-your-actual-api-key"
        print_color "$RED" "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo
    fi

    print_color "$GREEN" "Happy browsing! 🎉"
}

# Run main function
main
