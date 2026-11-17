# The image preview.sh builds the site in: Node as the emulators' local API
# has it (BASE, LOCAL_API_IMAGE in tools/repo/emulator-room.sh), git for the
# site's list of pages (src/site.mjs), and the site's packages from
# package-lock.json alone, by hash, with no install scripts run - the same on
# every computer. The checkout is copied in when it runs, not here.
ARG BASE
FROM ${BASE}
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /repo/website
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
