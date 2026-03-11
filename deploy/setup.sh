#!/bin/bash

# SC Recupero Crediti - Server Setup Script
# This script sets up a production server with Docker, Nginx, and SSL certificates

set -e  # Exit on error

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration variables
PROJECT_NAME="SC Recupero Crediti"
PROJECT_DIR="/opt/sc-recupero-crediti"
GITHUB_REPO="https://github.com/ferraboschi/sc-recupero-crediti.git"
DOMAIN="api-recupero.sakecompany.com"
BACKUP_DIR="/opt/backups/sc-recupero"
NGINX_LOG_DIR="/var/log/nginx/sc-recupero"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}$PROJECT_NAME - Server Setup${NC}"
echo -e "${GREEN}========================================${NC}"

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}This script must be run as root${NC}"
   exit 1
fi

# Step 1: Update system packages
echo -e "\n${YELLOW}Step 1: Updating system packages...${NC}"
apt-get update
apt-get upgrade -y

# Step 2: Install Docker
echo -e "\n${YELLOW}Step 2: Installing Docker...${NC}"
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh
    rm get-docker.sh
    echo -e "${GREEN}Docker installed${NC}"
else
    echo -e "${GREEN}Docker already installed${NC}"
fi

# Step 3: Install Docker Compose
echo -e "\n${YELLOW}Step 3: Installing Docker Compose...${NC}"
if ! command -v docker-compose &> /dev/null; then
    curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
    chmod +x /usr/local/bin/docker-compose
    echo -e "${GREEN}Docker Compose installed${NC}"
else
    echo -e "${GREEN}Docker Compose already installed${NC}"
fi

# Step 4: Install Nginx
echo -e "\n${YELLOW}Step 4: Installing Nginx...${NC}"
if ! command -v nginx &> /dev/null; then
    apt-get install -y nginx
    systemctl enable nginx
    echo -e "${GREEN}Nginx installed${NC}"
else
    echo -e "${GREEN}Nginx already installed${NC}"
fi

# Step 5: Install Certbot
echo -e "\n${YELLOW}Step 5: Installing Certbot...${NC}"
if ! command -v certbot &> /dev/null; then
    apt-get install -y certbot python3-certbot-nginx
    echo -e "${GREEN}Certbot installed${NC}"
else
    echo -e "${GREEN}Certbot already installed${NC}"
fi

# Step 6: Create project directory
echo -e "\n${YELLOW}Step 6: Creating project directory...${NC}"
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"
echo -e "${GREEN}Project directory created at $PROJECT_DIR${NC}"

# Step 7: Clone repository
echo -e "\n${YELLOW}Step 7: Cloning GitHub repository...${NC}"
if [ -d ".git" ]; then
    echo -e "${GREEN}Repository already exists, pulling latest changes...${NC}"
    git pull origin main
else
    git clone "$GITHUB_REPO" .
    echo -e "${GREEN}Repository cloned${NC}"
fi

# Step 8: Set up environment file
echo -e "\n${YELLOW}Step 8: Setting up environment configuration...${NC}"
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo -e "${YELLOW}Created .env from .env.example${NC}"
        echo -e "${RED}IMPORTANT: Please edit $PROJECT_DIR/.env with your configuration:${NC}"
        echo -e "  - DATABASE_URL (if using external database)"
        echo -e "  - SECRET_KEY (generate with: openssl rand -hex 32)"
        echo -e "  - DEBUG (set to False for production)"
        echo -e "  - ALLOWED_HOSTS (add $DOMAIN)"
        read -p "Press enter once you have configured .env..."
    else
        echo -e "${RED}ERROR: .env.example not found${NC}"
        exit 1
    fi
else
    echo -e "${GREEN}.env already configured${NC}"
fi

# Step 9: Create Nginx log directory
echo -e "\n${YELLOW}Step 9: Setting up Nginx logging...${NC}"
mkdir -p "$NGINX_LOG_DIR"
chown www-data:www-data "$NGINX_LOG_DIR"
chmod 755 "$NGINX_LOG_DIR"
echo -e "${GREEN}Log directory created at $NGINX_LOG_DIR${NC}"

# Step 10: Copy Nginx configuration
echo -e "\n${YELLOW}Step 10: Configuring Nginx...${NC}"
if [ -f "deploy/nginx.conf" ]; then
    cp deploy/nginx.conf /etc/nginx/sites-available/sc-recupero-crediti
    ln -sf /etc/nginx/sites-available/sc-recupero-crediti /etc/nginx/sites-enabled/sc-recupero-crediti

    # Remove default site if enabled
    rm -f /etc/nginx/sites-enabled/default

    # Test Nginx configuration
    if nginx -t; then
        echo -e "${GREEN}Nginx configuration is valid${NC}"
        systemctl restart nginx
    else
        echo -e "${RED}ERROR: Nginx configuration test failed${NC}"
        exit 1
    fi
else
    echo -e "${RED}ERROR: deploy/nginx.conf not found${NC}"
    exit 1
fi

# Step 11: Set up SSL with Certbot
echo -e "\n${YELLOW}Step 11: Setting up SSL certificate with Certbot...${NC}"
echo -e "${YELLOW}Note: Make sure $DOMAIN is pointing to this server's IP address${NC}"
read -p "Continue with SSL setup? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register --email admin@sakecompany.com
    echo -e "${GREEN}SSL certificate installed${NC}"
    systemctl restart nginx
else
    echo -e "${YELLOW}SSL setup skipped. You can run this later:${NC}"
    echo -e "  certbot --nginx -d $DOMAIN"
fi

# Step 12: Create backup directory
echo -e "\n${YELLOW}Step 12: Creating backup directory...${NC}"
mkdir -p "$BACKUP_DIR"
chmod 755 "$BACKUP_DIR"
echo -e "${GREEN}Backup directory created at $BACKUP_DIR${NC}"

# Step 13: Copy backup script
echo -e "\n${YELLOW}Step 13: Setting up database backup script...${NC}"
if [ -f "deploy/backup.sh" ]; then
    cp deploy/backup.sh /opt/sc-recupero-backup.sh
    chmod +x /opt/sc-recupero-backup.sh

    # Add to crontab (run daily at 2 AM)
    CRON_JOB="0 2 * * * /opt/sc-recupero-backup.sh"
    if ! crontab -l 2>/dev/null | grep -q "sc-recupero-backup.sh"; then
        (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
        echo -e "${GREEN}Backup script installed and scheduled (daily at 2 AM)${NC}"
    else
        echo -e "${GREEN}Backup script already in crontab${NC}"
    fi
else
    echo -e "${YELLOW}deploy/backup.sh not found, skipping${NC}"
fi

# Step 14: Start Docker containers
echo -e "\n${YELLOW}Step 14: Starting Docker containers...${NC}"
cd "$PROJECT_DIR"
docker-compose pull
docker-compose up -d
echo -e "${GREEN}Docker containers started${NC}"

# Step 15: Set up systemd service for auto-restart
echo -e "\n${YELLOW}Step 15: Creating systemd service for auto-restart...${NC}"
cat > /etc/systemd/system/sc-recupero-crediti.service <<'EOF'
[Unit]
Description=SC Recupero Crediti - Docker Compose Service
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/sc-recupero-crediti
ExecStart=/usr/local/bin/docker-compose up -d
ExecStop=/usr/local/bin/docker-compose down
RemainAfterExit=yes

# Auto-restart on reboot
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable sc-recupero-crediti.service
echo -e "${GREEN}Systemd service created and enabled${NC}"

# Step 16: Set up log rotation
echo -e "\n${YELLOW}Step 16: Setting up log rotation...${NC}"
cat > /etc/logrotate.d/sc-recupero-crediti <<EOF
$NGINX_LOG_DIR/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    create 0640 www-data www-data
    sharedscripts
    postrotate
        if [ -f /var/run/nginx.pid ]; then
            kill -USR1 \$(cat /var/run/nginx.pid)
        fi
    endscript
}

$PROJECT_DIR/logs/*.log {
    daily
    missingok
    rotate 30
    compress
    delaycompress
    notifempty
    create 0640 www-data www-data
}
EOF
echo -e "${GREEN}Log rotation configured${NC}"

# Step 17: Print summary
echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}Setup completed successfully!${NC}"
echo -e "${GREEN}========================================${NC}"
echo
echo -e "${GREEN}Project Information:${NC}"
echo -e "  Domain: https://$DOMAIN"
echo -e "  Project Directory: $PROJECT_DIR"
echo -e "  Backup Directory: $BACKUP_DIR"
echo -e "  Log Directory: $NGINX_LOG_DIR"
echo
echo -e "${GREEN}Next steps:${NC}"
echo -e "  1. Verify your application is running:"
echo -e "     curl http://localhost:8000/"
echo -e "  2. Check Docker status:"
echo -e "     docker-compose -f $PROJECT_DIR/docker-compose.yml ps"
echo -e "  3. View logs:"
echo -e "     docker-compose -f $PROJECT_DIR/docker-compose.yml logs -f"
echo -e "  4. Set up monitoring and alerts"
echo -e "  5. Configure database backups if needed"
echo
echo -e "${YELLOW}Useful commands:${NC}"
echo -e "  docker-compose -f $PROJECT_DIR/docker-compose.yml ps"
echo -e "  docker-compose -f $PROJECT_DIR/docker-compose.yml logs -f app"
echo -e "  docker-compose -f $PROJECT_DIR/docker-compose.yml restart"
echo -e "  certbot renew --dry-run"
echo
