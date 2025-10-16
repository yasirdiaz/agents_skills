nimport streamlit as st
import google.generativeai as genai
import json
from twilio.rest import Client
import os
import re

# --- CREDENTIALS CONFIGURATION (USING ENVIRONMENT VARIABLES) ---

# 1. Gemini Credentials
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-pro-latest')
else:
    # This message appears in the Streamlit app if the key is not set
    st.error("Configuration Error: The GEMINI_API_KEY was not found. Please set it in 'Secrets'.")
    model = None

# 2. Twilio Credentials
account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
workspace_sid = os.environ.get("TWILIO_WORKSPACE_SID")

client = None
if account_sid and auth_token and workspace_sid:
    try:
        # Initialize the client only if all credentials exist
        client = Client(account_sid, auth_token)
    except Exception as e:
        st.error(f"Error initializing the Twilio client. Please check the credentials. Error: {e}")
else:
    st.error("Configuration Error: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, or TWILIO_WORKSPACE_SID is missing.")


# --- TWILIO FUNCTIONS ---

def get_realtime_queue_stats(client, workspace_sid, target_skill_name):
    """
    Fetches real-time statistics for a specific Twilio queue.
    """
    if not client or not workspace_sid: return None

    try:
        task_queues = client.taskrouter.v1.workspaces(workspace_sid).task_queues.list()
        for queue in task_queues:
            if queue.friendly_name == target_skill_name:
                stats = client.taskrouter.v1.workspaces(workspace_sid).task_queues(queue.sid).statistics().fetch()
                return stats.realtime
    except Exception as e:
        st.error(f"Error fetching Twilio queue data: {e}")
    return None


def find_agent_skills(query, client, workspace_sid):
    """
    Finds the skills of a specific agent by their friendly name or email address.
    """
    if not client or not workspace_sid: return None

    try:
        workers = client.taskrouter.v1.workspaces(workspace_sid).workers.list()
        for worker in workers:
            # Parse worker attributes
            attributes = json.loads(worker.attributes)
            email = attributes.get('email', '')

            # Check for match by name or email
            if (worker.friendly_name.lower() == query.lower() or email.lower() == query.lower()):
                # Ensure the worker has the 'Agent' role
                if "Agent" in attributes.get('roles', []):
                    worker_skills = attributes.get('routing', {}).get('skills', [])
                    return ', '.join(worker_skills) if worker_skills else 'None'
    except Exception as e:
        st.error(f"Error finding agent skills: {e}")
        return "Error"
    return None


# --- AI FUNCTION ---
def generate_ai_response(history, new_message, twilio_data=None, agent_skills_data=None):
    """
    Generates an AI response based on conversation history and data.
    """
    if not model:
        return "The AI model is unavailable due to a configuration error."

    prompt_parts = [
        "You are a real-time analyst for all customer support queues at Wise. You are part of the Workforce Management team. You have access to check real-time information in Twilio about queues and agent skills. You can see which agents are assigned to which skills. When someone asks you to check the skills for an agent you need to check the Twilio skills assigned in real time to the agent you are asked to check.",
        "Use the conversation context to provide a relevant answer.",
        "If a user asks for an agent's skills (e.g., 'What skills does John have?' or 'What skills does john.doe@wise.com have?'), you MUST respond with 'ACTION_SEARCH_SKILLS: [Agent Name or Email]'. Do not add any other text.",
        f"Available Twilio data: {twilio_data}" if twilio_data else "",
        f"Available Agent Skills data: {agent_skills_data}" if agent_skills_data else "",
        "Conversation:",
    ]

    for turn in history:
        role = "User" if turn["role"] == "user" else "Assistant"
        prompt_parts.append(f"{role}: {turn['content']}")

    prompt_parts.append(f"User: {new_message}")
    prompt_parts.append("Assistant:")

    full_prompt = "\n".join(prompt_parts)

    try:
        response = model.generate_content(full_prompt)
        return response.text
    except Exception as e:
        return f"Error generating AI response: {e}"


# --- STREAMLIT INTERFACE ---
st.title("Twilio Agent Assistant (WFM)")

# Use session state to maintain conversation history
if "messages" not in st.session_state:
    st.session_state.messages = []

# Display previous messages
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Get user input
user_input = st.chat_input(
    "Enter your question for the bot (e.g., 'skills for John Doe' or 'stats for business voice')")

if user_input:
    # 1. Display the user's message
    with st.chat_message("user"):
        st.markdown(user_input)

    # 2. Process the bot's response
    with st.spinner("Thinking..."):
        # Add the user's message to history before calling the AI
        st.session_state.messages.append({"role": "user", "content": user_input})

        initial_ai_response = generate_ai_response(st.session_state.messages, user_input)
        final_response = ""

        # Logic for double AI call (Action Search Skills)
        if initial_ai_response.startswith("ACTION_SEARCH_SKILLS:") and client and workspace_sid:
            query = initial_ai_response.replace("ACTION_SEARCH_SKILLS:", "").strip()
            found_skills = find_agent_skills(query, client, workspace_sid)

            if found_skills is not None:
                # 2nd AI call with Twilio data
                data_for_ai = f"The skills for '{query}' are: {found_skills}"
                final_response = generate_ai_response(st.session_state.messages, user_input,
                                                      agent_skills_data=data_for_ai)
            else:
                final_response = f"Agent '{query}' not found or no skills available."

        # Logic for Queue Stats (Action Search Stats)
        else:
            queue_map = {
                "ts voice business": "Voice Total Service Business",
                "ts voice consumer": "Voice Total Service Consumer",
                "ts business": "Total Service Business",
                "ts consumer": "Total Service Consumer",
                "consumer voice": "Voice Global Consumer Primary",
                "consumer chats": "Chat Global Consumer Primary",
                "business voice": "Voice Global Business",
                "business chats": "Chat Global Business",
                "portuguese primary": "Language_Pt",
                "consumer emails hrsa": "Bucket_HRSA",
                "consumer emails svc": "Bucket_SVC",
                "primary business": "Profile_Business",
            }

            target_queue_name = None
            for keyword, queue_name in queue_map.items():
                if keyword in user_input.lower():
                    target_queue_name = queue_name
                    break

            if target_queue_name and client and workspace_sid:
                twilio_stats = get_realtime_queue_stats(client, workspace_sid, target_queue_name)
                # 2nd AI call with Twilio data
                final_response = generate_ai_response(st.session_state.messages, user_input, twilio_data=twilio_stats)
            else:
                # If no action is needed, the initial response is the final one
                final_response = initial_ai_response

    # 3. Display the bot's final response
    with st.chat_message("assistant"):
        st.markdown(final_response)

    # 4. Add the final bot response to history

    st.session_state.messages.append({"role": "assistant", "content": final_response})
