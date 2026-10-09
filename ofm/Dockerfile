# syntax=docker/dockerfile:1.7
# Free LLM endpoint for ark-watch news NLP: Ebony-Vinyl/dsh-our-free-model, `packages/standalone`.
# Third-party MIT code, pinned by COMMIT and fetched at build time (never vendored in this public
# repo). Security audit of this exact commit: no runtime deps, no self-update on the standalone
# path, prompts only go to opencode.ai and api.kilo.ai. One patch below disables the sealed EAC path.
FROM scratch AS src
ADD https://github.com/Ebony-Vinyl/dsh-our-free-model.git#befe36559e9f642ddac279e8b275558367f9f085 /src

# node 22.23.3, digest-pinned (engines needs ^22.19)
FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402
WORKDIR /app
COPY --from=src /src/packages /app/packages
COPY --from=src /src/src /app/src
COPY --from=src /src/vendor /app/vendor
COPY --from=src /src/package.json /app/package.json
COPY relay.mjs start.sh /app/
# EAC: its AES-GCM sealed credential is decrypted at startup regardless of host; make that default
# a no-op. The build FAILS if the patch did not apply (upstream changed) so it can never ship unpatched.
RUN sed -i 's/credentialOf = openSeal/credentialOf = () => null/' /app/packages/standalone/eac.mjs \
 && grep -q 'credentialOf = () => null' /app/packages/standalone/eac.mjs \
 && mkdir -p /data && chown -R node:node /data /app
USER node
ENV OFM_HOME=/data NODE_ENV=production
EXPOSE 18901
CMD ["sh", "/app/start.sh"]
