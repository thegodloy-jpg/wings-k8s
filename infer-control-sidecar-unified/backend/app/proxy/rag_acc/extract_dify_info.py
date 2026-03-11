import re
from fastchat.protocol.openai_api_protocol import ChatCompletionRequest
from app.proxy.settings import logger


def is_dify_scenario(chat_input: ChatCompletionRequest) -> bool:
    if len(chat_input.messages) != 2:
        return False
    system_msg = None
    user_msg = None
    for msg in chat_input.messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            system_msg = msg
        elif isinstance(msg, dict) and msg.get("role") == "user":
            user_msg = msg
    if system_msg and "<context>" in system_msg.get("content", "") and "</context>" in system_msg.get("content", ""):
        return True
    return False


def extract_dify_info(chat_input: ChatCompletionRequest):
    if not is_dify_scenario(chat_input):
        return None
    system_prompt = ""
    user_question = ""
    rag_documents = []
    for msg in chat_input.messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            system_prompt = msg.get("content", "")
            content = msg.get("content", "")
            logger.info(f"Raw content: {repr(content)}")
            if isinstance(content, str):
                context_match = re.search(r'<context>\n(.*?)\n</context>', content, re.DOTALL)
                if context_match:
                    context_content = context_match.group(1)
                    logger.info(f"Extracted context_content: {repr(context_content)}")
                    doc_chunks = context_content.split('\n\n')
                    doc_chunks = [item.strip() for item in doc_chunks if item.strip()]
                    rag_documents.extend(doc_chunks)
                else:
                    logger.info("No <context> tag found in content.")
        elif isinstance(msg, dict) and msg.get("role") == "user":
            user_question = msg.get("content", "")
    return {
        "rag_documents": rag_documents,
        "system_prompt": system_prompt,
        "user_question": user_question
    }
