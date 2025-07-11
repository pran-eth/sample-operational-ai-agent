import json
import os
import boto3
import logging
import yaml
import traceback
import time
import uuid
import random
from datetime import datetime, timedelta
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth
from strands import Agent
from strands.models import BedrockModel
from opentelemetry.context.contextvars_context import ContextVarsRuntimeContext
from opentelemetry import context

from agent_tools.send_incident_email import send_incident_email
from agent_tools.send_approval_email import send_approval_email
from agent_tools.query_metrics import query_metrics
from agent_tools.check_recent_deployment import check_recent_deployment
from agent_tools.correlate_errors import correlate_errors_across_services
from agent_tools.post_incident_summary import post_incident_summary
from agent_tools.query_logs import query_logs
from agent_tools.store_agent_finding import store_agent_finding
from deploymentSpecialist import handle_deployment_issue
from agent_tools.send_approval_email import send_approval_email

# Simple in-memory cache with TTL
query_cache = {}
CACHE_TTL = 300  # 5 minutes
def load_config():
    """Load configuration from config.yaml file."""
    # First check if CONFIG_PATH environment variable is set
    env_config_path = os.environ.get('CONFIG_PATH')
    
    config_paths = []
    if env_config_path:
        config_paths.append(env_config_path)
    
    # Add standard paths
    config_paths.extend([
        os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.yaml'),  # Project root
        os.path.join(os.path.dirname(__file__), 'config.yaml'),  # Legacy path
        '/var/task/config.yaml',  # Lambda deployment package root
    ])
    
    import tempfile
    for config_path in config_paths:
        try:
            # Use secure temporary file handling
            with tempfile.NamedTemporaryFile(mode='w+', delete=True) as temp:
                with open(config_path, 'r') as file:
                    config_content = file.read()
                    temp.write(config_content)
                    temp.flush()
                    temp.seek(0)
                    config = yaml.safe_load(temp)
                    return config
        except Exception as e:
            logger.debug(f"Could not load config from {config_path}: {str(e)}")
    
    # Try to load from environment variable if file not found
    config_json = os.environ.get('CONFIG_JSON', '')
    if config_json:
        try:
            config = json.loads(config_json)
          #  logger.info("Loaded config from CONFIG_JSON environment variable")
            return config
        except Exception as e:
            logger.debug(f"Could not load config from CONFIG_JSON: {str(e)}")
    
    logger.error("Could not load config from any location")
    return None

# Initialize Bedrock model
try:
    secret = get_secret()
    if secret and 'strands' in secret:
        model_id = secret['strands'].get('model_id', "anthropic.claude-3-5-sonnet-20240620-v1:0")
        region = secret['strands'].get('region', 'us-east-1')
    else:
        model_id = "anthropic.claude-3-5-sonnet-20240620-v1:0"
        region = os.environ.get('AWS_REGION', 'us-east-1')
except NameError:
    # Fallback if get_secret is not defined
    model_id = "anthropic.claude-3-5-sonnet-20240620-v1:0"
    region = os.environ.get('AWS_REGION', 'us-east-1')


bedrock_model = BedrockModel(
    model_id=model_id,
    region_name=region
)

# Initialize agents
monitoring_agent = Agent(
    model=bedrock_model,
    tools=[
        send_incident_email,
        send_approval_email,
        query_metrics,
        check_recent_deployment,
        correlate_errors_across_services,
        handle_deployment_issue,
        store_agent_finding,
        send_approval_email
    ]
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("oasis")

# Initialize clients

# Simple in-memory cache with TTL
query_cache = {}
CACHE_TTL = 300  # 5 minutes

def get_from_cache(cache_key):
    """Get item from cache if it exists and is not expired."""
    if cache_key in query_cache:
        item = query_cache[cache_key]
        if item['expiry'] > time.time():
            return item['data']
    return None

def add_to_cache(cache_key, data):
    """Add item to cache with expiration."""
    query_cache[cache_key] = {
        'data': data,
        'expiry': time.time() + CACHE_TTL
    }

def get_secret():
    """Get configuration from AWS Secrets Manager."""
    secret_name = os.environ.get('OASIS_SECRET_NAME', 'oasis-configuration')
    secret_region = os.environ.get('OASIS_SECRET_REGION', os.environ.get('AWS_REGION', 'us-east-1'))
    
    try:
        # Get secret from AWS Secrets Manager
        logger.info(f"Getting configuration from Secrets Manager: {secret_name}")
        secrets_client = boto3.client('secretsmanager', region_name=secret_region)
        secret_response = secrets_client.get_secret_value(SecretId=secret_name)
        return json.loads(secret_response['SecretString'])
    except Exception as e:
        logger.error(f"Error getting secret from Secrets Manager: {str(e)}")
        return None

def get_opensearch_client():
    """Create and return an OpenSearch client."""
    logger.info("Creating OpenSearch client")
    
    # Try to get configuration from Secrets Manager
    secret = get_secret()
    
    if secret and 'opensearch' in secret:
        # Use credentials from Secrets Manager
        os_config = secret['opensearch']
        endpoint = os_config.get('endpoint')
        username = os_config.get('username')
        password = os_config.get('password')
        region = os_config.get('region', 'us-east-1')
        index_prefix = os_config.get('index_prefix', 'app-logs')
        
        # Store index_prefix in environment variable for other functions to use
        os.environ['INDEX_PREFIX'] = index_prefix
        
        # Extract hostname from endpoint URL
        host = endpoint.replace('https://', '')
        
        # Create OpenSearch client with basic auth
        http_auth = (username, password)
        client = OpenSearch(
            hosts=[{'host': host, 'port': 443}],
            http_auth=http_auth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection
        )
        return client
    else:
           # AWS SigV4 authentication
        credentials = boto3.Session().get_credentials()
        awsauth = AWS4Auth(
                credentials.access_key,
                credentials.secret_key,
                region,
                'es',
                session_token=credentials.token
            )
        return OpenSearch(
                hosts=[{'host': host, 'port': 443}],
                http_auth=awsauth,
                use_ssl=True,
                verify_certs=True,
                connection_class=RequestsHttpConnection
            )
def get_baseline_error_rates(client, index, service=None, days=7):
    """Get baseline error rates for comparison."""
    end_time = datetime.utcnow() - timedelta(days=1)  # Exclude last day
    start_time = end_time - timedelta(days=days)
    
    start_time_str = start_time.isoformat() + 'Z'
    end_time_str = end_time.isoformat() + 'Z'
    
    # Create cache key
    cache_key = f"baseline_{service}_{start_time_str}_{end_time_str}"
    cached_result = get_from_cache(cache_key)
    if cached_result:
        return cached_result
    
    query = {
        "size": 0,
        "query": {
            "bool": {
                "must": [
                    {"range": {"timestamp": {"gte": start_time_str, "lte": end_time_str}}},
                    {"term": {"level": "ERROR"}}
                ]
            }
        },
        "aggs": {
            "errors_per_day": {
                "date_histogram": {
                    "field": "timestamp",
                    "calendar_interval": "day"
                }
            }
        }
    }
    
    # Add service filter if specified
    if service:
        query["query"]["bool"]["must"].append({"term": {"service": service}})
    
    try:
        response = client.search(body=query, index=index)
        
        # Calculate average errors per day
        buckets = response["aggregations"]["errors_per_day"]["buckets"]
        if not buckets:
            return {"avg_per_day": 0, "avg_per_hour": 0, "avg_per_minute": 0}
        
        total_errors = sum(bucket["doc_count"] for bucket in buckets)
        days_with_data = len(buckets)
        
        avg_per_day = total_errors / days_with_data if days_with_data > 0 else 0
        avg_per_hour = avg_per_day / 24
        avg_per_minute = avg_per_hour / 60
        
        result = {
            "avg_per_day": avg_per_day,
            "avg_per_hour": avg_per_hour,
            "avg_per_minute": avg_per_minute
        }
        
        # Cache the result
        add_to_cache(cache_key, result)
        return result
        
    except Exception as e:
        logger.error(f"Error getting baseline error rates: {str(e)}")
        return {"avg_per_day": 0, "avg_per_hour": 0, "avg_per_minute": 0}
def check_recent_deployment(client, index, service, hours=24):
    """Check if there was a recent deployment for the service."""
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(hours=hours)
    
    start_time_str = start_time.isoformat() + 'Z'
    end_time_str = end_time.isoformat() + 'Z'
    
    # Create cache key
    cache_key = f"deployment_{service}_{start_time_str}_{end_time_str}"
    cached_result = get_from_cache(cache_key)
    if cached_result:
        return cached_result
    
    # Query for deployment-related logs
    query = {
        "size": 1,
        "query": {
            "bool": {
                "must": [
                    {"range": {"timestamp": {"gte": start_time_str, "lte": end_time_str}}},
                    {"term": {"service": service}},
                    {"bool": {
                        "should": [
                            {"match_phrase": {"message": "deployment"}},
                            {"match_phrase": {"message": "deployed"}},
                            {"match_phrase": {"message": "version"}},
                            {"match_phrase": {"message": "update"}},
                            {"match_phrase": {"message": "upgraded"}},
                            {"match_phrase": {"message": "rollout"}},
                            {"match_phrase": {"message": "release"}}
                        ],
                        "minimum_should_match": 1
                    }}
                ]
            }
        },
        "sort": [{"timestamp": {"order": "desc"}}]
    }
    
    try:
        response = client.search(body=query, index=index)
        hits = response["hits"]["hits"]
        
        if hits:
            deployment = {
                "found": True,
                "timestamp": hits[0]["_source"]["timestamp"],
                "message": hits[0]["_source"]["message"]
            }
        else:
            deployment = {"found": False}
        
        # Cache the result
        add_to_cache(cache_key, deployment)
        return deployment
        
    except Exception as e:
        logger.error(f"Error checking recent deployment: {str(e)}")
        return {"found": False}
def check_for_errors(timeframe_minutes=15, error_threshold=10):
    """Check OpenSearch for error logs with optimized aggregations."""
    logger.info(f"Starting error check for last {timeframe_minutes} minutes with threshold {error_threshold}")
    
    try:
        client = get_opensearch_client()
        logger.info("Successfully created OpenSearch client")
    except Exception as e:
        logger.error(f"Failed to create OpenSearch client: {str(e)}")
        raise
    
    # Try to get index prefix from config file first
    secret = get_secret()
    os_config = secret['opensearch']
    index_prefix = os_config.get('index_prefix', 'app-logs')
    index = index_prefix + '-logs'
    
    # Calculate time range
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(minutes=timeframe_minutes)
    
    # Format timestamps for OpenSearch using ISO format
    start_time_str = start_time.isoformat() + 'Z'
    end_time_str = end_time.isoformat() + 'Z'
    
    # Create cache key
    cache_key = f"errors_{start_time_str}_{end_time_str}_{error_threshold}"
    cached_result = get_from_cache(cache_key)
    if cached_result:
        return cached_result
    
    # Optimized query with aggregations
    query = {
        "size": 0,
        "query": {
            "bool": {
                "must": [
                    {"range": {"timestamp": {"gte": start_time_str, "lte": end_time_str}}},
                    {"match": {"level": "ERROR"}}
                ]
            }
        },
        "aggs": {
            "errors_over_time": {
                "date_histogram": {
                    "field": "timestamp",
                    "fixed_interval": "1m"
                }
            },
            "by_service": {
                "terms": {
                    "field": "service",
                    "size": 10
                },
                "aggs": {
                    "by_error_type": {
                        "terms": {
                            "field": "error_type",
                            "size": 10
                        }
                    },
                    "error_samples": {
                        "top_hits": {
                            "size": 3,
                            "_source": ["timestamp", "message", "error_type", "status_code"],
                            "sort": [{"timestamp": {"order": "desc"}}]
                        }
                    },
                    "errors_over_time": {
                        "date_histogram": {
                            "field": "timestamp",
                            "fixed_interval": "1m"
                        }
                    }
                }
            }
        }
    }
    
    try:
        response = client.search(body=query, index=index)
        
        # Get total error count
        total_errors = response["hits"]["total"]["value"]
        
        if total_errors >= error_threshold:
            # Process time-based error distribution
            time_buckets = response["aggregations"]["errors_over_time"]["buckets"]
            error_timeline = []
            for bucket in time_buckets:
                error_timeline.append({
                    "timestamp": bucket["key_as_string"],
                    "count": bucket["doc_count"]
                })
            
            # Calculate error rate per minute
            minutes_with_data = len([b for b in time_buckets if b["doc_count"] > 0])
            current_error_rate = total_errors / max(1, minutes_with_data)
            
            # Process service-based error distribution
            services_with_errors = []
            for bucket in response["aggregations"]["by_service"]["buckets"]:
                service_name = bucket["key"]
                error_count = bucket["doc_count"]
                
                # Get error types for this service
                error_types = {}
                for error_bucket in bucket["by_error_type"]["buckets"]:
                    error_type = error_bucket["key"]
                    error_types[error_type] = error_bucket["doc_count"]
                
                # Get sample error messages
                error_samples = []
                for hit in bucket["error_samples"]["hits"]["hits"]:
                    source = hit["_source"]
                    error_samples.append({
                        "timestamp": source.get("timestamp"),
                        "message": source.get("message"),
                        "error_type": source.get("error_type"),
                        "status_code": source.get("status_code")
                    })
                
                # Get error timeline for this service
                service_timeline = []
                for time_bucket in bucket["errors_over_time"]["buckets"]:
                    if time_bucket["doc_count"] > 0:
                        service_timeline.append({
                            "timestamp": time_bucket["key_as_string"],
                            "count": time_bucket["doc_count"]
                        })
                
                # Get baseline error rate for this service
                baseline = get_baseline_error_rates(client, index, service_name)
                
                # Check for recent deployment
                deployment = check_recent_deployment(client, index, service_name)
                
                services_with_errors.append({
                    "service": service_name,
                    "error_count": error_count,
                    "error_types": error_types,
                    "error_samples": error_samples,
                    "timeline": service_timeline,
                    "baseline": baseline,
                    "recent_deployment": deployment
                })
            
            result = {
                "total_errors": total_errors,
                "current_error_rate": current_error_rate,
                "timeframe": {
                    "start": start_time_str,
                    "end": end_time_str,
                    "minutes": timeframe_minutes
                },
                "error_timeline": error_timeline,
                "services_with_errors": services_with_errors
            }
            
            # Cache the result
            add_to_cache(cache_key, result)
            return result
        
        return None
    
    except Exception as e:
        logger.error(f"Error querying OpenSearch: {str(e)}")
        logger.error(f"Error details: {traceback.format_exc()}")
        return None
def check_service_dependencies(service):
    """Get service dependencies from config."""
    # Try to get dependencies from config file first
    config = load_config()
    if config and 'services' in config:
        # Build dependencies map from config
        dependencies = {}
        for svc in config['services']:
            if 'name' in svc and 'dependencies' in svc:
                dependencies[svc['name']] = svc['dependencies']
        
        return dependencies.get(service, [])
    else:
        # Fall back to hardcoded mapping
        dependencies = {
            "api-gateway": ["auth-service", "product-service"],
            "auth-service": ["user-db"],
            "product-service": ["product-db", "cache-service"],
            "user-db": [],
            "product-db": [],
            "cache-service": []
        }
        
        return dependencies.get(service, [])
def generate_contextual_prompt(error_data):
    """Generate a contextual prompt for the agent based on error patterns."""
    if not error_data or not error_data.get("services_with_errors"):
        return "No significant errors detected."
    
    # Find the most affected service
    most_affected_service = max(error_data["services_with_errors"], 
                               key=lambda x: x["error_count"])
    
    service_name = most_affected_service["service"]
    error_count = most_affected_service["error_count"]
    current_rate = error_data["current_error_rate"]
    baseline_rate = most_affected_service["baseline"]["avg_per_minute"]
    
    # Get top error types
    error_types = most_affected_service["error_types"]
    top_errors = sorted(error_types.items(), key=lambda x: x[1], reverse=True)
    top_errors_str = ", ".join([f"{err_type} ({count})" for err_type, count in top_errors[:3]])
    
    # Check for error spike
    error_increase = current_rate / max(0.1, baseline_rate)
    
    # Check for recent deployment
    deployment_info = most_affected_service["recent_deployment"]
    recent_deployment = deployment_info["message"] if deployment_info.get("found") else "None"
    
    # Get dependencies
    dependencies = check_service_dependencies(service_name)
    dependencies_str = ", ".join(dependencies) if dependencies else "None"
    
    # Get sample error messages
    error_samples = most_affected_service.get("error_samples", [])
    sample_messages = "\n".join([f"- {sample.get('error_type', 'Unknown')}: {sample.get('message', 'No message')}" 
                               for sample in error_samples[:2]])
    
    # Build the prompt
    prompt = f"""
I've detected an anomaly in service monitoring that requires analysis:
Incident Overview:
SERVICE INFORMATION:
- Primary affected service: {service_name}
- Current error count: {error_count} errors in {error_data['timeframe']['minutes']} minutes
- Current error rate: {current_rate:.2f}/minute (baseline: {baseline_rate:.2f}/minute)
- Error increase: {error_increase:.1f}x normal rate
- Top error types: {top_errors_str}
- Service dependencies: {dependencies_str}

TIMEFRAME:
- Start time: {error_data['timeframe']['start']}
- End time: {error_data['timeframe']['end']}
- Duration: {error_data['timeframe']['minutes']} minutes
- Use timeframe: "last_{error_data['timeframe']['minutes']}m" for query_metrics tool

ERROR SAMPLES:
{sample_messages}

DEPLOYMENT CONTEXT:
- Recent deployment: {recent_deployment}

ANALYSIS TASKS:
1. Root Cause Analysis:
   - Analyze the provided information to determine the most likely root cause
   - Identify any contributing factors
   - Assess the confidence level of your determination (high/medium/low)

2. Data Correlation:
   - If needed, gather and analyze correlation data to support your findings
   - Specify which additional data points would be valuable, if any

3. Metrics Evaluation:
   - Collect and analyze relevant metrics
   - Highlight any anomalies or patterns in the data

4. Deployment Impact Assessment:
   - Evaluate if the issue is related to any recent deployments
   - If so, pinpoint specific changes that may have contributed
   - DO NOT use handle_deployment_issue tool without explicit human approval

5. Mitigation Strategy:
   - Recommend specific, actionable steps to address the root cause
   - Prioritize these steps based on impact and ease of implementation
   - DO NOT implement any actions without human approval

6. Monitoring Enhancements:
   - Suggest improvements to monitoring systems to detect similar issues earlier
   - Include specific metrics, thresholds, or alerts that should be implemented

7. Update findings in Opensearch:
   - Store your findings and recommendations in OpenSearch

8. IMPORTANT - Human Approval Required:
   - Send approval email to get explicit human approval BEFORE taking ANY action
   - Clearly state all proposed actions that require approval
   - Wait for human approval before implementing any changes
   - NEVER restart services, rollback deployments, or make system changes without approval

9. Comprehensive Email Communication:
   - Send detailed HTML Incident Analysis Report in email with good formatting
"""
    
    return prompt
 

def progressive_analysis(error_data):
    """Perform progressive analysis using multiple agent interactions."""
    if not error_data or not error_data.get("services_with_errors"):
        return {"status": "no_errors", "message": "No significant errors detected."}
    
    # Generate a session ID for this analysis
    session_id = f"auto-monitor-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    
    # Step 1: Initial error assessment
    initial_prompt = generate_contextual_prompt(error_data)
    
    try:
        # Enable DEBUG logs for the tool registry only
        logging.getLogger("strands.tools.registry").setLevel(logging.DEBUG)
        # Set WARNING level for model interactions
        logging.getLogger("strands.models").setLevel(logging.DEBUG)

        response = monitoring_agent(initial_prompt)
        # Return completed status
        return {
            "status": "completed"
        }
        
    except Exception as e:
        logger.error(f"Error in progressive analysis: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            "status": "error",
            "message": f"Error during analysis: {str(e)}",
            "error_data": error_data
        }

def lambda_handler(event, context):
    """
    AWS Lambda handler for auto-monitoring.
    
    This function is triggered periodically to check for errors and invoke the agent if needed.
    It also handles approval/rejection requests from API Gateway.
    """
    logger.info("Lambda function started")
    logger.info(f"Event: {json.dumps(event)}")
    logger.info(f"Lambda function ARN: {context.invoked_function_arn}")
    logger.info(f"Lambda function version: {context.function_version}")
    
    # Log environment variables (excluding sensitive data)
    env_vars = {k: v for k, v in os.environ.items() 
                if not any(sensitive in k.lower() for sensitive in ['password', 'secret', 'key', 'token'])}
    
    try:
        # Check if this is an API Gateway request for approval/rejection
        if event.get('queryStringParameters'):
            query_params = event.get('queryStringParameters')
            finding_id = query_params.get('finding_id')
            action = query_params.get('action')
            
            if finding_id and action in ['approve', 'reject']:
                logger.info(f"Processing {action} request for finding {finding_id}")
                
                # Get OpenSearch client
                client = get_opensearch_client()
                secret = get_secret()
                os_config = secret['opensearch']
                index_prefix = os_config.get('index_prefix', 'app-logs')
                index_name = f"{index_prefix}-agent-findings"
                
                try:
                    # First check the current status of the finding
                    try:
                        response = client.get(index=index_name, id=finding_id)
                        finding = response["_source"]
                        current_status = finding.get("status", "")
                        
                        # Don't proceed if already in approved/processed state
                        if current_status in ["approved", "processed"]:
                            logger.info(f"Finding {finding_id} is already in {current_status} state. No action needed.")
                            return {
                                'statusCode': 200,
                                'headers': {'Content-Type': 'text/html'},
                                'body': f"""
                                <!DOCTYPE html>
                                <html>
                                <head>
                                    <title>No Action Needed</title>
                                    <style>
                                        body {{ font-family: Arial, sans-serif; margin: 40px; text-align: center; }}
                                        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 5px; }}
                                        .info {{ color: #2196F3; }}
                                    </style>
                                </head>
                                <body>
                                    <div class="container">
                                        <h1 class="info">No Action Needed</h1>
                                        <p>The finding {finding_id} is already in {current_status} state.</p>
                                    </div>
                                </body>
                                </html>
                                """
                            }
                    except Exception as e:
                        logger.warning(f"Could not retrieve finding {finding_id}: {str(e)}")
                        # Continue with the process if we can't retrieve the finding
                
                    # Update the finding with approval status
                    approved = (action == 'approve')
                    feedback = f"Action {'approved' if approved else 'rejected'} via email link."
                    status = "approved" if approved else "rejected"
                    
                    try:
                        # Update the document in OpenSearch with retry logic
                        max_retries = 3
                        retry_count = 0
                        
                        while retry_count < max_retries:
                            try:
                                # Get the latest version of the document
                                latest = client.get(index=index_name, id=finding_id)
                                seq_no = latest['_seq_no']
                                primary_term = latest['_primary_term']
                                
                                # Update with version control parameters
                                client.update(
                                    index=index_name,
                                    id=finding_id,
                                    body={
                                        "doc": {
                                            "human_feedback": feedback,
                                            "human_approved": approved,
                                            "status": status,
                                            "updated_at": datetime.now().isoformat()
                                        }
                                    },
                                    if_seq_no=seq_no,
                                    if_primary_term=primary_term,
                                    refresh=True
                                )
                                break  # Success, exit the retry loop
                            except Exception as update_error:
                                retry_count += 1
                                if retry_count >= max_retries:
                                    raise  # Re-raise the exception if max retries reached
                                logger.warning(f"Update conflict, retrying ({retry_count}/{max_retries}): {str(update_error)}")
                    except Exception as e:
                        logger.error(f"Failed to update finding after {max_retries} attempts: {str(e)}")
                        raise
                    
                    # If approved, execute the actions immediately
                    if approved:
                        try:
                            # Get the finding details (refresh after update)
                            response = client.get(index=index_name, id=finding_id)
                            finding = response["_source"]
                            
                            logger.info(f"Executing approved actions for finding {finding_id}")
                            logger.info(f"Actions to execute: {finding.get('proposed_actions')}")
                            
                            # Update the finding status to processed with version control
                            try:
                                # Get the latest version of the document
                                latest = client.get(index=index_name, id=finding_id)
                                seq_no = latest['_seq_no']
                                primary_term = latest['_primary_term']
                                
                                client.update(
                                    index=index_name,
                                    id=finding_id,
                                    body={
                                        "doc": {
                                            "status": "processed",
                                            "processed_at": datetime.now().isoformat()
                                        }
                                    },
                                    if_seq_no=seq_no,
                                    if_primary_term=primary_term,
                                    refresh=True
                                )
                            except Exception as update_error:
                                logger.warning(f"Failed to update finding status: {str(update_error)}")
                                # Continue execution even if update fails
                            
                            # Create a prompt for the agent to execute the actions
                            action_prompt = f"""
                            Execute the following approved actions for incident {finding_id}:
                            
                            {finding.get('proposed_actions')}
                            
                            Service affected: {finding.get('related_resources', {}).get('service')}
                            Error count: {finding.get('related_resources', {}).get('error_count')}
                            Use agent tool handle_deployment_issue for actions.
                            Please execute these actions and send a email summary of what was done.
                            """
                            
                            action_response = monitoring_agent(action_prompt)
                            
                            # Update the finding status to completed after agent completes the action
                            try:
                                # Get the latest version of the document
                                latest = client.get(index=index_name, id=finding_id)
                                seq_no = latest['_seq_no']
                                primary_term = latest['_primary_term']
                                
                                client.update(
                                    index=index_name,
                                    id=finding_id,
                                    body={
                                        "doc": {
                                            "status": "processed",
                                            "completed_at": datetime.now().isoformat(),
                                            "action_response": action_response
                                        }
                                    },
                                    if_seq_no=seq_no,
                                    if_primary_term=primary_term,
                                    refresh=True
                                )
                            except Exception as update_error:
                                logger.warning(f"Failed to update finding completion status: {str(update_error)}")
                                # Continue execution even if update fails
                            logger.info(f"Updated finding {finding_id} status to completed after agent action")
                            
                        except Exception as e:
                            # Update the finding status to failed if there was an error
                            try:
                                # Get the latest version of the document
                                latest = client.get(index=index_name, id=finding_id)
                                seq_no = latest['_seq_no']
                                primary_term = latest['_primary_term']
                                
                                client.update(
                                    index=index_name,
                                    id=finding_id,
                                    body={
                                        "doc": {
                                            "status": "failed",
                                            "failed_at": datetime.now().isoformat(),
                                            "error_message": str(e)
                                        }
                                    },
                                    if_seq_no=seq_no,
                                    if_primary_term=primary_term,
                                    refresh=True
                                )
                            except Exception as update_error:
                                logger.warning(f"Failed to update finding failure status: {str(update_error)}")
                                # Continue execution even if update fails
                            logger.error(f"Error executing actions for finding {finding_id}: {str(e)}")
                    
                    # Return a simple HTML response
                    html_response = f"""
                    <!DOCTYPE html>
                    <html>
                    <head>
                        <title>Action Processed</title>
                        <style>
                            body {{ font-family: Arial, sans-serif; margin: 40px; text-align: center; }}
                            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 5px; }}
                            .success {{ color: #4CAF50; }}
                            .info {{ color: #2196F3; }}
                        </style>
                    </head>
                    <body>
                        <div class="container">
                            <h1 class="{'success' if approved else 'info'}">Action {'Approved' if approved else 'Rejected'}</h1>
                            <p>The proposed actions for finding {finding_id} have been {'approved' if approved else 'rejected'}.</p>
                            <p>{'The system will now execute the approved actions.' if approved else 'No actions will be taken.'}</p>
                        </div>
                    </body>
                    </html>
                    """
                    
                    return {
                        'statusCode': 200,
                        'headers': {'Content-Type': 'text/html'},
                        'body': html_response
                    }
                    
                except Exception as e:
                    logger.error(f"Error updating finding {finding_id}: {str(e)}")
                    return {
                        'statusCode': 500,
                        'headers': {'Content-Type': 'text/html'},
                        'body': f"<html><body><h1>Error</h1><p>An error occurred: {str(e)}</p></body></html>"
                    }
            
            # Invalid parameters
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'text/html'},
                'body': "<html><body><h1>Error</h1><p>Invalid request. Missing finding ID or invalid action.</p></body></html>"
            }
        
        
        
        # Get configuration from event or environment variables
        check_interval = event.get('check_interval', 30)  # Get from event or use 24 hours (1440 minutes) as default
        error_threshold = int(os.environ.get('ERROR_THRESHOLD', '1'))  # Lower threshold to 1
        logger.info(f"Using check_interval={check_interval}, error_threshold={error_threshold}")
        
        # Check for errors
        logger.info("Starting error check")
        error_data = check_for_errors(check_interval, error_threshold)
        
        if error_data:
            # Errors exceeded threshold, perform progressive analysis
            analysis_result = progressive_analysis(error_data)
            
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'Error threshold exceeded, analysis completed',
                    'status': analysis_result['status'],
                    'error_count': error_data['total_errors'],
                    'finding_id': analysis_result.get('finding_id')
                })
            }
        else:
            # No errors or below threshold
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'No significant errors detected'
                })
            }
    
    except Exception as e:
        logger.error(f"Error in auto-monitoring: {str(e)}")
        logger.error(traceback.format_exc())
        return {
            'statusCode': 500,
            'body': json.dumps({
                'message': f'Error in auto-monitoring: {str(e)}'
            })
        }
