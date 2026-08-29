FROM python:3.12-slim
WORKDIR /site
COPY index.html config.js api.js store.js views.js app.js ./
EXPOSE 5173
CMD ["python3", "-m", "http.server", "5173"]
