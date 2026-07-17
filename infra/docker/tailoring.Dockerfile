# Tailoring Lambda container image. Bundles the Typst binary (resume render)
# alongside the Python package, on the AWS Lambda Python base image.
# Build context is the repo root.
FROM public.ecr.aws/lambda/python:3.12

# Typst binary for deterministic PDF rendering.
ARG TYPST_VERSION=0.12.0
RUN dnf install -y tar xz && \
    curl -fsSL "https://github.com/typst/typst/releases/download/v${TYPST_VERSION}/typst-x86_64-unknown-linux-musl.tar.xz" \
      -o /tmp/typst.tar.xz && \
    tar -xJf /tmp/typst.tar.xz -C /tmp && \
    cp /tmp/typst-x86_64-unknown-linux-musl/typst /usr/local/bin/typst && \
    chmod +x /usr/local/bin/typst && \
    rm -rf /tmp/typst*

# Install the project (all src/ components) into the Lambda task root.
COPY pyproject.toml ${LAMBDA_TASK_ROOT}/_src/pyproject.toml
COPY src ${LAMBDA_TASK_ROOT}/_src/src
COPY config ${LAMBDA_TASK_ROOT}/config
RUN pip install --no-cache-dir ${LAMBDA_TASK_ROOT}/_src -t ${LAMBDA_TASK_ROOT} && \
    rm -rf ${LAMBDA_TASK_ROOT}/_src

ENV APPLIEDIN_CONFIG_DIR=config
CMD ["tailoring.handler.handler"]
