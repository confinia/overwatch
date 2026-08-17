# Runner image for the Selenium IDE walk (rule 1: nothing is installed on the
# Mac or the VM — the browser, the driver and the runner all live in here).
#
# Chromium and its driver come from Debian so their versions always match; a
# npm-installed chromedriver drifts from the distro browser and fails with
# "This version of ChromeDriver only supports Chrome version N".
FROM docker.io/library/node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium chromium-driver ca-certificates python3 python3-requests tini \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g selenium-side-runner@4.0.4

ENV CHROME_BIN=/usr/bin/chromium
WORKDIR /work
ENTRYPOINT ["/usr/bin/tini", "--"]
