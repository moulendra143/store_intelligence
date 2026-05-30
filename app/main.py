from fastapi import FastAPI

app = FastAPI(
    title="Store Intelligence API",
    version="1.0.0"
)

@app.get("/")
def root():
    return {
        "message": "Store Intelligence API Running"
    }

@app.get("/health")
def health():
    return {
        "status": "healthy"
    }