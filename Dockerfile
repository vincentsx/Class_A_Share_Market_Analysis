FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY pyproject.toml ./
COPY stock_daily ./stock_daily
RUN useradd --create-home --uid 10001 researcher && mkdir /app/data && chown researcher:researcher /app/data
USER researcher
EXPOSE 8000
CMD ["python", "-m", "stock_daily", "serve", "--host", "0.0.0.0"]

