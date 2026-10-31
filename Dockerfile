FROM python:3.12-slim
WORKDIR /app
COPY app.py index.html ./
COPY rules/ rules/
RUN useradd --system --no-create-home polishr
USER polishr
EXPOSE 8080
CMD ["python3", "app.py"]
