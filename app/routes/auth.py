"""
Authentication API endpoints.
"""

import logging
from fastapi import APIRouter, HTTPException, Header, Depends, Request
from typing import Optional

from app.models.user import (
    UserCreate, UserLogin, UserResponse, 
    TokenPair, TokenRefreshRequest, AuthResponse
)
from app.services.auth_service import AuthService
from app.database.db_service import DatabaseService
from app.utils.cache import cache_user, get_cached_user

# Import event system (Phase 2)
try:
    from app.events import publisher as event_publisher
except Exception as e:
    event_publisher = None
    print(f"[!] Warning: Could not import event publisher: {e}")

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create router
router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])

# Initialize services
db_service = DatabaseService()
auth_service = AuthService(db_service)


def get_token_from_header(authorization: Optional[str] = Header(None)) -> Optional[str]:
    """Extract token from Authorization header"""
    if not authorization:
        return None
    
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    
    return parts[1]


@router.post("/register", response_model=AuthResponse)
async def register(user_data: UserCreate):
    """
    Register new user account.
    
    Creates user with hashed password, generates JWT tokens,
    and creates session.
    
    Args:
        user_data: UserCreate with email, password, company_name
        
    Returns:
        AuthResponse with user info and token pair
    """
    try:
        logger.info(f"Registration attempt for email: {user_data.email}")
        
        result = await auth_service.register(user_data)
        
        # Publish user.created event (Phase 2)
        if event_publisher:
            try:
                event_publisher.publish_user_created(
                    user_id=str(result.user.user_id),
                    email=result.user.email,
                    username=result.user.email.split("@")[0]
                )
                logger.info(f"[✓] Published user.created event for {result.user.user_id}")
            except Exception as e:
                logger.warning(f"[!] Failed to publish user.created event: {e}")
        
        logger.info(f"User registered successfully: {result.user.user_id}")
        return result
        
    except ValueError as e:
        logger.warning(f"Registration failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Registration error: {e}")
        raise HTTPException(status_code=500, detail="Registration failed")


@router.post("/login", response_model=AuthResponse)
async def login(credentials: UserLogin, request: Request):
    """
    Login with email and password.
    
    Validates credentials, generates JWT tokens, and creates session.
    
    Args:
        credentials: UserLogin with email and password
        request: FastAPI Request object for getting client IP
        
    Returns:
        AuthResponse with user info and token pair
    """
    try:
        logger.info(f"Login attempt for email: {credentials.email}")
        
        result = await auth_service.login(credentials)
        
        # Publish auth.login event (Phase 2)
        if event_publisher:
            try:
                client_ip = request.client.host if request.client else "unknown"
                event_publisher.publish_auth_login(
                    user_id=str(result.user.user_id),
                    ip_address=client_ip
                )
                logger.info(f"[✓] Published auth.login event for {result.user.user_id}")
            except Exception as e:
                logger.warning(f"[!] Failed to publish auth.login event: {e}")
        
        logger.info(f"User logged in successfully: {result.user.user_id}")
        return result
        
    except ValueError as e:
        # Publish login failure event
        if event_publisher:
            try:
                client_ip = request.client.host if request.client else "unknown"
                event_publisher.publish_auth_failed(
                    email=credentials.email,
                    reason="invalid_credentials",
                    ip_address=client_ip
                )
                logger.info(f"[✓] Published auth.failed event for {credentials.email}")
            except Exception as e:
                logger.warning(f"[!] Failed to publish auth.failed event: {e}")
        
        logger.warning(f"Login failed: {e}")
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=500, detail="Login failed")


@router.post("/refresh", response_model=TokenPair)
async def refresh_token(request: TokenRefreshRequest):
    """
    Refresh access token using refresh token.
    
    Validates refresh token and session, generates new access token.
    
    Args:
        request: TokenRefreshRequest with refresh_token
        
    Returns:
        TokenPair with new access token and same refresh token
    """
    try:
        result = await auth_service.refresh_access_token(request.refresh_token)
        
        logger.info("Access token refreshed successfully")
        return result
        
    except ValueError as e:
        logger.warning(f"Token refresh failed: {e}")
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        raise HTTPException(status_code=500, detail="Token refresh failed")


@router.post("/logout")
async def logout(request: TokenRefreshRequest):
    """
    Logout user by invalidating session.
    
    Deletes session from database, invalidating refresh token.
    
    Args:
        request: TokenRefreshRequest with refresh_token
        
    Returns:
        Success message
    """
    try:
        success = await auth_service.logout(request.refresh_token)
        
        # Publish auth.logout event (Phase 2)
        if success and event_publisher:
            try:
                # Try to extract user_id from refresh token if possible
                user_info = await auth_service.get_user_from_refresh_token(request.refresh_token)
                if user_info:
                    event_publisher.publish_auth_logout(user_id=str(user_info.get("user_id", "unknown")))
                    logger.info(f"[✓] Published auth.logout event")
            except Exception as e:
                logger.warning(f"[!] Failed to publish auth.logout event: {e}")
        
        if success:
            logger.info("User logged out successfully")
            return {"success": True, "message": "Logged out successfully"}
        else:
            raise HTTPException(status_code=400, detail="Logout failed")
        
    except Exception as e:
        logger.error(f"Logout error: {e}")
        raise HTTPException(status_code=500, detail="Logout failed")


@router.get("/me", response_model=UserResponse)
async def get_current_user(authorization: Optional[str] = Header(None)):
    """
    Get current user info from access token.
    
    Validates access token and returns user information.
    
    Args:
        authorization: Bearer token in Authorization header
        
    Returns:
        UserResponse with user info
    """
    try:
        token = get_token_from_header(authorization)
        if not token:
            raise HTTPException(status_code=401, detail="No authorization token provided")
        
        user = await auth_service.get_current_user(token)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        
        return user
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get current user error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get user info")


@router.post("/migrate-ids")
async def migrate_ids(
    diagnostic_id: Optional[str] = None,
    snapshot_id: Optional[str] = None,
    blueprint_id: Optional[str] = None,
    authorization: Optional[str] = Header(None)
):
    """
    Migrate localStorage IDs to user account.
    
    Links existing diagnostic/snapshot/blueprint records to user account.
    Called automatically after login/signup from frontend.
    
    Args:
        diagnostic_id: Diagnostic ID from localStorage
        snapshot_id: Snapshot ID from localStorage
        blueprint_id: Blueprint ID from localStorage
        authorization: Bearer token in Authorization header
        
    Returns:
        Migration status for each ID type
    """
    try:
        token = get_token_from_header(authorization)
        if not token:
            raise HTTPException(status_code=401, detail="No authorization token provided")
        
        user = await auth_service.get_current_user(token)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        
        migrated = await auth_service.migrate_ids_to_user(
            user.user_id,
            diagnostic_id,
            snapshot_id,
            blueprint_id
        )
        
        logger.info(f"IDs migrated for user {user.user_id}: {migrated}")
        
        return {
            "success": True,
            "migrated": migrated,
            "message": "IDs migrated successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"ID migration error: {e}")
        raise HTTPException(status_code=500, detail="ID migration failed")
