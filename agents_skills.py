import streamlit as st
import google.generativeai as genai
import json
from twilio.rest import Client
import os
import re

# --- CREDENTIALS CONFIGURATION (USING ENVIRONMENT VARIABLES) ---
# Security maintained to load keys from the environment

# 1. Gemini Credentials
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-pro-latest')
else:
    st.error("Configuration Error: The GEMINI_API_KEY was not found. Please set it in 'Secrets'.")
    model = None

# 2. Twilio Credentials
account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
workspace_sid = os.environ.get("TWILIO_WORKSPACE_SID")

# Twilio client initialization using caching (@st.cache_resource)
# This function is used to create and cache the complex Client object.
@st.cache_resource
def get_twilio_client(sid, token):
    """Initializes and caches the Twilio Client object."""
    try:
        if sid and token:
            return Client(sid, token)
    except Exception as e:
        st.error(f"Error initializing the Twilio client. Please check credentials. Error: {e}")
    return None

# The client is not initialized globally, but will be called inside cached functions.

if not account_sid or not auth_token or not workspace_sid:
    st.error("Configuration Error: Missing Twilio credentials (SID or Token/Workspace SID).")


# --- TWILIO FUNCTION (WITH CACHING & FIX FOR UnhashableParamError) ---

@st.cache_data(ttl=600)  # Caches skills for 10 minutes
def find_agent_skills(query, workspace_sid):  # <-- FIXED: 'client' argument removed
    """
    Finds agent skills by their name or email address.
    """
    # Get the cached client instance internally (solves the UnhashableParamError)
    client = get_twilio_client(account_sid, auth_token)

    if not client or not workspace_sid: return None
    
    try:
        workers = client.taskrouter.v1.workspaces(workspace_sid).workers.list()
        for worker in workers:
            attributes = json.loads(worker.attributes)
            email = attributes.get('email', '')

            # Search by name or email
            if (worker.friendly_name.lower() == query.lower() or email.lower() == query.lower()):
                if "Agent" in attributes.get('roles', []):
                    worker_skills = attributes.get('routing', {}).get('skills', [])
                    return ', '.join(worker_skills) if worker_skills else 'None'
    except Exception as e:
        st.error(f"Error searching for agent skills: {e}")
        return "Error"
    return None


# --- AI FUNCTION (Only for formatting the final response) ---

def generate_ai_response(history, new_message, agent_skills_data=None):
    """
    Generates a natural language response based on the skill search result.
    """
    if not model:
        return "The AI model is unavailable due to a configuration error."

    prompt_parts = [
        "You are a Workforce Management (WFM) assistant whose SOLE JOB is to inform agents about their assigned routing skills. You must always be polite and direct. Only generate the response in English.",
        "All the people that will use resource is to check the skills assigned to agents of customer service, so every time that someones reaches out you must ask for the email of the agent they want to check.",
        "Once they reply with the email of the agent, you must inmediately initiate the search of the skills in real time in Twilio for the email associated to the agent and you will reply ONLY when yon find the skills.",
        "I will provide the skills information. Your job is to reformat it into a user-friendly response. Never mention Twilio or TaskRouter.",
        f"Skills Data: {agent_skills_data}",
        "Conversation:",
    ]

    for turn in history:
        # Role translation is only for internal prompt history
        role = "Agent" if turn["role"] == "user" else "Assistant"
        prompt_parts.append(f"{role}: {turn['content']}")

    prompt_parts.append(f"Agent: {new_message}")
    prompt_parts.append("Assistant:")

    full_prompt = "\n".join(prompt_parts)

    try:
        response = model.generate_content(full_prompt)
        return response.text
    except Exception as e:
        return f"Error generating AI response: {e}"


# --- STREAMLIT INTERFACE AND STATE LOGIC ---

st.title("Agent Skills Assistant (WFM)")
st.caption("Hello! I'm your skills assistant. Ask me about your assigned skills.")

# Initialize state and messages
if "messages" not in st.session_state:
    st.session_state.messages = []
if "state" not in st.session_state:
    st.session_state.state = 'INITIAL' # States: 'INITIAL', 'WAITING_FOR_EMAIL', 'SEARCHING'


# Display previous messages
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Show initial greeting if conversation starts
if not st.session_state.messages and st.session_state.state == 'INITIAL':
    initial_greeting = "Hello! I am the Skills Assistant. Which agent or email would you like to check skills for?"
    with st.chat_message("assistant"):
        st.markdown(initial_greeting)
    st.session_state.messages.append({"role": "assistant", "content": initial_greeting})


# Get user input
user_input = st.chat_input("Write your query or the agent's email...")

if user_input:
    # 1. Display the user's message
    with st.chat_message("user"):
        st.markdown(user_input)
    st.session_state.messages.append({"role": "user", "content": user_input})

    # 2. Process the bot's response
    with st.spinner("Searching..."):
        
        final_response = ""
        query = user_input.strip() 
        
        # --- MAIN LOGIC BASED ON STATE ---

        # If we are waiting for an email, the current input is the query
        if st.session_state.state == 'WAITING_FOR_EMAIL':
            st.session_state.state = 'SEARCHING' # Move to search state
        
        # If in INITIAL state and user provided a general query, ask for email
        elif st.session_state.state == 'INITIAL' and "@" not in user_input:
            st.session_state.state = 'WAITING_FOR_EMAIL'
            final_response = "Understood. For a more accurate search, please provide ONLY the agent's email address (e.g., agent@wise.com)."
            
        # If the state is SEARCHING (or was INITIAL and contained '@')
        if st.session_state.state == 'SEARCHING' or ("@" in query and st.session_state.state == 'INITIAL'):
            
            # Perform search for skills in Twilio (uses caching!)
            # FIXED CALL: Only pass hashable arguments
            found_skills = find_agent_skills(query, workspace_sid) 

            if found_skills is not None:
                # Generate final response with AI
                data_for_ai = f"The agent {query} has the following skills: {found_skills}"
                final_response = generate_ai_response(st.session_state.messages, user_input, agent_skills_data=data_for_ai)
            else:
                # Fallback error response
                final_response = f"Agent '{query}' not found or no skills are assigned. Please verify the email and try again."
            
            # Reset the state of the conversation after the search
            st.session_state.state = 'INITIAL'

        # 3. Display the bot's response
        with st.chat_message("assistant"):
            st.markdown(final_response)
        
        # 4. Add the final bot response to history
        st.session_state.messages.append({"role": "assistant", "content": final_response})

