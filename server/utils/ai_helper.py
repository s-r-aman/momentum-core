import os

from langchain_openai.chat_models import ChatOpenAI
from portkey_ai import createHeaders, PORTKEY_GATEWAY_URL

color_prefix_by_role = {
    "system": "\033[0m",  # gray
    "human": "\033[0m",  # gray
    "user": "\033[0m",  # gray
    "ai": "\033[92m",  # green
    "assistant": "\033[92m",  # green
}


def get_llm_client(user_id, model_name):
    return create_client("openai", os.environ.get("OPENAI_API_KEY"), model_name, user_id)

def create_client(provider, key, model_name, user_id):
    if provider == "openai":
        PORTKEY_API_KEY = os.environ.get("PORTKEY_API_KEY")
        PROVIDER_API_KEY = key

        portkey_headers = createHeaders(api_key=PORTKEY_API_KEY,provider="openai", metadata={"_user":user_id, "environment":os.environ.get("ENV")})

        return ChatOpenAI(api_key=PROVIDER_API_KEY, model=model_name, base_url=PORTKEY_GATEWAY_URL, default_headers=portkey_headers)
        


def llm_call(client, messages, print_text=True, temperature=0.4):
    response = client(messages=messages, temperature=temperature)
    if print_text:
        print_message_delta(response)
    return response


def print_messages(
    messages, color_prefix_by_role=color_prefix_by_role
) -> None:
    """Prints messages sent to or from GPT."""
    for message in messages:
        role = message.type
        color_prefix = color_prefix_by_role[role]
        content = message.content
        print(f"{color_prefix}\n[{role}]\n{content}")


def print_message_delta(
    delta, color_prefix_by_role=color_prefix_by_role
) -> None:
    """Prints a chunk of messages streamed back from GPT."""
    role = delta.type
    color_prefix = color_prefix_by_role[role]
    print(f"{color_prefix}\n[{role}]\n", end="")
    content = delta.content
    print(content, end="")


def print_message_delta_openai(
    delta, color_prefix_by_role=color_prefix_by_role
) -> None:
    """Prints a chunk of messages streamed back from GPT."""
    role = delta.role
    color_prefix = color_prefix_by_role[role]
    print(f"{color_prefix}\n[{role}]\n", end="")
    content = delta.content
    print(content, end="")
