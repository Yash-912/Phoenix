from fastapi import FastAPI

from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Demo Checkout Service")


@app.get("/")
def root():
    return {"service": "checkout", "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/checkout")
def checkout():
    return {"order_id": "demo-order-1", "status": "confirmed"}


Instrumentator().instrument(app).expose(app)
