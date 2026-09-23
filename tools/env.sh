# Source this before any dotnet/uv command: `source tools/env.sh`
export DOTNET_ROOT="$HOME/.dotnet"
export PATH="$HOME/.dotnet:$HOME/.dotnet/tools:$HOME/.local/bin:$PATH"
export DOTNET_ROLL_FORWARD=LatestMajor   # ilspycmd 8.2 targets an older runtime
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2"
export STS2_DATA_DIR="$STS2_GAME_DIR/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
