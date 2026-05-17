FROM ollama/ollama:latest
COPY entrypoint.sh /entrypoint.sh
# Strip Windows CRLF line endings in case git checked out with autocrlf=true
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh
EXPOSE 11434
ENTRYPOINT ["/entrypoint.sh"]
