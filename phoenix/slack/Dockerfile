FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY slack/requirements.txt slack-requirements.txt
RUN pip install --no-cache-dir -r slack-requirements.txt

# The package keeps its layout, phoenix/slack, so it runs as `python -m phoenix.slack.bridge`
# and its imports are the same inside the image as on the host. Nothing else from the
# repository goes in: not the graph, not the API, not the tools.
COPY __init__.py phoenix/__init__.py
COPY slack phoenix/slack

RUN useradd --create-home --shell /bin/false phoenix
USER phoenix

CMD ["python", "-m", "phoenix.slack.bridge"]
