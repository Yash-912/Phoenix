FROM python:3.12-slim

WORKDIR /app

# Artifact identity and the regression under test are baked into the image, not
# supplied at runtime. Two build args produce two genuinely different artifacts:
#   v17: APP_VERSION=v17  REGRESSION_ENABLED=false
#   v18: APP_VERSION=v18  REGRESSION_ENABLED=true
# Nothing that happens to a running container can change either value, so a
# restart cannot "fix" v18 -- only redeploying v17 can.
ARG APP_VERSION=dev
ARG REGRESSION_ENABLED=false

ENV APP_VERSION=${APP_VERSION}
ENV REGRESSION_ENABLED=${REGRESSION_ENABLED}

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
