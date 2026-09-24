FROM python:3.12-alpine
WORKDIR /app
COPY app.py .
RUN adduser -D relay
USER relay
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/health || exit 1
CMD ["python", "-u", "app.py"]
