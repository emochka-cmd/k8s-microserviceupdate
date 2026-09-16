from app import create_app

app = create_app()

if __name__ == "__main__":
    # Только для локальной отладки. В проде/в k8s используется gunicorn.
    app.run(host="0.0.0.0", port=5000)
