FROM python:3.12-slim
RUN addgroup --system app && adduser --system --ingroup app app
WORKDIR /srv
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
USER app
EXPOSE 8400
CMD ["uvicorn", "--factory", "mcprouter.api.app:create_app", "--host", "0.0.0.0", "--port", "8400"]
