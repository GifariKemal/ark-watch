#!/bin/sh
node /app/relay.mjs &
exec node /app/packages/standalone/cli.mjs --port 18900
