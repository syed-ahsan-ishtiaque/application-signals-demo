import json
import boto3
import random
import uuid
from opentelemetry import trace
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')
table_name = 'HistoricalRecordDynamoDBTable'
table = dynamodb.Table(table_name)

MAX_RETRIES = 3
BACKOFF_BASE = 0.1

def lambda_handler(event, context):

    query_params = event.get('queryStringParameters', {})
    current_span = trace.get_current_span()
    owner_id = random.randint(1, 9)
    current_span.set_attribute("owner.id", owner_id)

    record_id = query_params.get('recordId')
    owners = query_params.get('owners')
    pet_id = query_params.get('petid')

    if record_id is None:
        return {
            'statusCode': 400,
            'body': json.dumps({'error': 'Missing recordId'})
        }

    ticket_id = str(uuid.uuid4())
    
    for attempt in range(MAX_RETRIES):
        try:
            table.put_item(
                Item={
                    'ticket_id': ticket_id,
                    'recordId': record_id,
                    'value': 'Case Report ' + record_id + ': Acute Gastroenteritis in a 3-Year-Old Female Labrador Retriever'
                },
                ConditionExpression='attribute_not_exists(ticket_id)'
            )
            return {
                'statusCode': 200,
                'body': json.dumps({'message': 'Item added successfully', 'recordId': record_id, 'ticketId': ticket_id})
            }
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                if attempt < MAX_RETRIES - 1:
                    import time
                    backoff = BACKOFF_BASE * (2 ** attempt)
                    time.sleep(backoff)
                    ticket_id = str(uuid.uuid4())
                    continue
                else:
                    return {
                        'statusCode': 409,
                        'body': json.dumps({'error': 'Failed to create unique ticket after retries', 'recordId': record_id})
                    }
            else:
                return {
                    'statusCode': 500,
                    'body': json.dumps({'error': str(e)})
                }
        except Exception as e:
            return {
                'statusCode': 500,
                'body': json.dumps({'error': str(e)})
            }
