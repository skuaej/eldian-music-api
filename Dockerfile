FROM python:3.13-slim

WORKDIR /app

# Install ffmpeg, nodejs (JS runtime), curl, unzip, and Deno
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deno.land/install.sh | sh

# Configure Deno and Local bin in PATH
ENV DENO_INSTALL="/root/.deno"
ENV PATH="${DENO_INSTALL}/bin:/root/.local/bin:${PATH}"

# Install uv for fast dependency management
RUN curl -Ls https://astral.sh/uv/install.sh | sh

# Copy dependencies
COPY requirements.txt .

# Install dependencies using uv pip
RUN uv pip install --system --no-cache -r requirements.txt

# Copy all project files
COPY . .

# Environment configuration
ENV PORT=8000
EXPOSE 8000

# Start script
CMD ["bash", "start"]
