# Tailoring Lambda container image. Bundles the Typst binary (resume render)
# alongside the Python package, on the AWS Lambda Python base image.
# Build context is the repo root.
FROM public.ecr.aws/lambda/python:3.12

# Tectonic: single self-contained LaTeX engine for résumé PDF rendering.
ARG TECTONIC_VERSION=0.15.0
RUN dnf install -y tar xz && \
    curl -fsSL "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
      -o /tmp/tectonic.tar.gz && \
    tar -xzf /tmp/tectonic.tar.gz -C /usr/local/bin && \
    chmod +x /usr/local/bin/tectonic && \
    rm -rf /tmp/tectonic*

# Install the project (all src/ components) into the Lambda task root.
COPY pyproject.toml ${LAMBDA_TASK_ROOT}/_src/pyproject.toml
COPY src ${LAMBDA_TASK_ROOT}/_src/src
COPY config ${LAMBDA_TASK_ROOT}/config
RUN pip install --no-cache-dir ${LAMBDA_TASK_ROOT}/_src -t ${LAMBDA_TASK_ROOT} && \
    rm -rf ${LAMBDA_TASK_ROOT}/_src

ENV APPLIEDIN_CONFIG_DIR=config
CMD ["tailoring.handler.handler"]
