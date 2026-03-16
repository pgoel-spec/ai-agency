# 1. Base Image: Microsoft's Playwright image (Includes Node.js, npm, and all Linux browser GUI dependencies)
FROM mcr.microsoft.com/playwright:v1.58.2-jammy

# 2. System Dependencies: Install Java (Required for Firebase Emulators), Python, and Git
RUN apt-get update && apt-get install -y \
    default-jre \
    python3 \
    python3-pip \
    python3-venv \
    git \
    curl \
    libnspr4 \
    libnss3 \
    libxss1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# 3. Global Backend Tools: Install Firebase CLI globally so the agent doesn't struggle with 'npx' paths
RUN npm install -g firebase-tools@latest

# 4. Global Testing Tools: Pre-install the testing libraries we discussed so they are instantly available
RUN npm install -g jest supertest jest-environment-jsdom ts-node typescript

# 5. Pre-warm NPM Cache (Optional but highly recommended)
# By installing the most common heavy Firebase packages globally, we warm the local npm cache. 
# When the agent runs `npm install` inside its cloned repo later, npm will pull from this local cache 
# instantly instead of downloading hundreds of megabytes from the internet.
RUN npm install -g firebase-admin firebase-functions express cors

# Set the default working directory
WORKDIR /home/user