FROM python:3.12.10-slim

WORKDIR /app
COPY requirements.txt requirements-rsi.txt ./
RUN pip install --no-cache-dir -r requirements-rsi.txt
COPY load_graph.py chatbot.py ./
COPY .streamlit ./.streamlit
COPY rsi ./rsi

CMD ["python", "load_graph.py"]
