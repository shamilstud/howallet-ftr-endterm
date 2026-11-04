#!/usr/bin/env bash
# Tear down the FTR Endterm lab cluster.
set -euo pipefail
kind delete cluster --name ftr-endterm
