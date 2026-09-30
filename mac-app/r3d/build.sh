#!/bin/sh
set -e
cd "$(dirname "$0")"
SDK="${R3DSDK:-$HOME/Downloads/R3DSDKv9_2_1}"
xcrun clang++ -O2 -std=c++17 -I"$SDK/Include" kickoff_r3d.cpp "$SDK/Lib/mac64/libR3DSDK-libcpp.a" \
  -DKICKOFF_DEFAULT_LIBS="\"$SDK/Redistributable/mac\"" -ldl -o kickoff_r3d
