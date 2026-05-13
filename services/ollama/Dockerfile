FROM ollama/ollama:latest

# Pull phi3-mini on image build so it is available immediately on first start.
RUN ollama serve & sleep 5 && ollama pull phi3-mini && pkill ollama

EXPOSE 11434
CMD ["ollama", "serve"]
