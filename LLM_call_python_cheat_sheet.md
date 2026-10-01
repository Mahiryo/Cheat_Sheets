# LLM API call examples


## Table of Contents

- [OpenAI](#openai)
- [Anthropic](#anthropic)
- [Cohere](#cohere)
- [Mistral](#mistral)
- [Google](#google)
- [Groq](#groq)

## OpenAI

🔑 Get API key [here](https://platform.openai.com/account/api-keys).

📃 API [docs](https://platform.openai.com/docs/).

### Chat
```python
import requests
import os

response = requests.post(
    "https://api.openai.com/v1/chat/completions",
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"
    },
    json={
        "model": "gpt-3.5-turbo",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"}
        ]
    }
)
```

### Embeddings
```python
import requests
import os

response = requests.post(
    "https://api.openai.com/v1/embeddings",
    headers={
        "Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY"),
        "Content-Type": "application/json"
    },
    json={
        "input": "The food was delicious and the wine...",
        "model": "text-embedding-ada-002",
        "encoding_format": "float"
    }
)
```

## Anthropic

🔑 Get API key [here](https://console.anthropic.com/account/keys).

📃 API [docs](https://docs.anthropic.com/).

### Chat
```python
import requests
import os

response = requests.post(
    "https://api.anthropic.com/v1/complete",
    headers={
        "accept": "application/json",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "x-api-key": os.environ.get("ANTHROPIC_API_KEY")
    },
    json={
        "model": "claude-2.1",
        "prompt": "\n\nHuman: Hello, world!\n\nAssistant:",
        "max_tokens_to_sample": 256
    }
)
```

## Cohere

🔑 Get API key [here](https://dashboard.cohere.com/api-keys).

📃 API [docs](https://docs.cohere.com/).

### Chat
```python
import requests
import os

response = requests.post(
    "https://api.cohere.ai/v1/chat",
    headers={
        "accept": "application/json",
        "content-type": "application/json",
        "Authorization": f"Bearer {os.environ['COHERE_API_KEY']}"
    },
    json={
        "chat_history": [
            {"role": "USER", "message": "Who discovered gravity?"},
            {"role": "CHATBOT", "message": "The man who is widely credited with discovering gravity is Sir Isaac Newton"}
        ],
        "message": "What year was he born?",
        "connectors": [{"id": "web-search"}]
    }
)
```

### Embeddings
```python
import requests
import os

response = requests.post(
    "https://api.cohere.ai/v1/embed",
    headers={
        "accept": "application/json",
        "content-type": "application/json",
        "Authorization": f"Bearer {os.environ['COHERE_API_KEY']}"
    },
    json={
        "texts": [
            "hello",
            "goodbye"
        ],
        "truncate": "END"
    }
)
```

## Mistral

🔑 Get API key [here](https://console.mistral.ai/users/api-keys/).

📃 API [docs](https://docs.mistral.ai/api/).

### Chat
```python
import requests
import os

response = requests.post(
    "https://api.mistral.ai/v1/chat/completions",
    headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {os.environ.get('MISTRAL_API_KEY')}"
    },
    json={
        "model": "mistral-tiny",
        "messages": [{"role": "user", "content": "Who is the most renowned French writer?"}]
    }
)
```

### Embeddings
```python
import requests
import os

response = requests.post(
    "https://api.mistral.ai/v1/embeddings",
    headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {os.environ['MISTRAL_API_KEY']}"
    },
    json={
        "model": "mistral-embed",
        "input": ["Embed this sentence.", "As well as this one."]
    }
)
```

## Google

🔑 Get API key [here](https://makersuite.google.com/app/apikey).

📃 API [docs](https://ai.google.dev/api/rest).

### Chat
```python
import requests
import os

response = requests.post(
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key=" + os.environ["GOOGLE_API_KEY"],
    headers={
        "Content-Type": "application/json"
    },
    json={
        "contents": [
            {
                "parts": [
                    {
                        "text": "Write a story about a magic backpack."
                    }
                ]
            }
        ]
    }
)
```

### Embeddings
```python
import requests
import os

response = requests.post(
    "https://generativelanguage.googleapis.com/v1beta/models/embedding-001:generateContent?key=" + os.environ.get("GOOGLE_API_KEY"),
    headers={
        "Content-Type": "application/json"
    },
    json={
        "contents": [
            {
                "parts": [
                    {
                        "text": "This is a sentence."
                    }
                ]
            }
        ]
    }
)
```

## Groq

🔑 Get API key [here](https://console.groq.com/keys).

📃 API [docs](https://console.groq.com/docs/).

### Chat
```python
import requests
import os

response = requests.post(
    "https://api.groq.com/openai/v1/chat/completions",
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"
    },
    json={
        "model": "mixtral-8x7b-32768",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"}
        ]
    }
)
```
