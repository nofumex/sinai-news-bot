FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml ./
COPY requirements.lock ./
COPY sinai ./sinai
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home sinai && mkdir /app/data /app/config && chown -R sinai:sinai /app
USER sinai
EXPOSE 8000
CMD ["sinai", "serve"]
