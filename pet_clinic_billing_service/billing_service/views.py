from rest_framework import viewsets, status
from rest_framework.response import Response
from django.db.models import Subquery, Count, Sum
from django.utils import timezone
from django.core.cache import cache
from .models import Billing,CheckList
from .serializers import BillingSerializer
from opentelemetry import trace
import logging
import boto3
import datetime
import os
import json
import time

logger = logging.getLogger(__name__)

_INVALID_NAMES_CACHE = None
_INVALID_NAMES_CACHE_TIME = None
_CACHE_TTL = 300

class BillingViewSet(viewsets.ViewSet):
    def list(self, request):
        global _INVALID_NAMES_CACHE, _INVALID_NAMES_CACHE_TIME
        
        logger.info("BillingViewSet.list() called - Fetching billing records")
        span = trace.get_current_span()

        FIXED_SUBQUERY_LIMIT = int(os.getenv("SUBQUERY_LIMIT", 100))
        
        current_time = time.time()
        if (_INVALID_NAMES_CACHE is None or 
            _INVALID_NAMES_CACHE_TIME is None or 
            (current_time - _INVALID_NAMES_CACHE_TIME) > _CACHE_TTL):
            _INVALID_NAMES_CACHE = list(
                CheckList.objects.values_list('invalid_name', flat=True).distinct()[:FIXED_SUBQUERY_LIMIT]
            )
            _INVALID_NAMES_CACHE_TIME = current_time
            logger.debug(f"Refreshed invalid_names cache with {len(_INVALID_NAMES_CACHE)} entries")

        MAX_RESULTS = int(os.getenv("MAX_BILLING_RESULTS", 1000))
        logger.info(f"Query parameters - subquery_limit: {FIXED_SUBQUERY_LIMIT}, max_results: {MAX_RESULTS}")
        
        qs = Billing.objects.exclude(type_name__in=_INVALID_NAMES_CACHE)[:MAX_RESULTS]

        db_start = time.time()
        objs = list(qs)  
        db_duration_ms = (time.time() - db_start) * 1_000
        record_count = len(objs)
        logger.info(f"Database query completed - Records: {record_count}, Duration: {db_duration_ms:.2f}ms")
        
        span.set_attribute("db.subquery_limit", FIXED_SUBQUERY_LIMIT)
        span.set_attribute("db.record_count", record_count)
        span.set_attribute("db.fetch_time_ms", db_duration_ms)

        ser_start = time.time()
        serializer = BillingSerializer(objs, many=True)
        ser_duration_ms = (time.time() - ser_start) * 1_000
        logger.debug(f"Serialization completed - Duration: {ser_duration_ms:.2f}ms")
        span.set_attribute("serialization.time_ms", ser_duration_ms)

        logger.info(f"BillingViewSet.list() completed successfully - Returned {record_count} records")
        return Response(serializer.data)

    def retrieve(self, request, pk=None, owner_id=None, type=None, pet_id=None):
        logger.info(f"BillingViewSet.retrieve() called - pk: {pk}, owner_id: {owner_id}, type: {type}, pet_id: {pet_id}")
        try:
            billing_obj = None
            if pk is not None:
                logger.debug(f"Retrieving billing record by ID: {pk}")
                billing_obj = Billing.objects.get(id=pk)
            else:
                logger.debug(f"Retrieving billing record by owner_id: {owner_id}, type: {type}, pet_id: {pet_id}")
                billing_obj = Billing.objects.get(owner_id=owner_id, type=type, pet_id=pet_id)
            
            serializer = BillingSerializer(billing_obj)
            logger.info(f"BillingViewSet.retrieve() completed successfully - Found billing record")
            return Response(serializer.data)
        except Billing.DoesNotExist:
            logger.warning(f"BillingViewSet.retrieve() - Billing object not found with given parameters")
            return Response({'message': 'Billing object not found'}, status=404)

    def create(self, request):
        logger.info(f"BillingViewSet.create() called - Creating new billing record")
        logger.debug(f"Request data: {request.data}")
        
        serializer = BillingSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            logger.info(f"BillingViewSet.create() - Billing record created successfully, ID: {serializer.data.get('id')}")
            self.log(request.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        
        logger.error(f"BillingViewSet.create() - Validation failed: {serializer.errors}")
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def update(self, request, pk=None):
        logger.info(f"BillingViewSet.update() called - Updating billing record ID: {pk}")
        logger.debug(f"Request data: {request.data}")
        
        try:
            billing_obj = Billing.objects.get(id=pk)
            serializer = BillingSerializer(billing_obj, data=request.data)
            if serializer.is_valid():
                serializer.save()
                logger.info(f"BillingViewSet.update() - Billing record updated successfully, ID: {pk}")
                self.log(request.data)
                return Response(serializer.data)
            
            logger.error(f"BillingViewSet.update() - Validation failed: {serializer.errors}")
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        except Billing.DoesNotExist:
            logger.warning(f"BillingViewSet.update() - Billing object not found with ID: {pk}")
            return Response({'message': 'Billing object not found'}, status=status.HTTP_404_NOT_FOUND)

    def log(self, data):
        logger.info(f"BillingViewSet.log() called - Logging billing data to DynamoDB")
        try:
            region = os.environ.get('REGION', 'us-east-1')
            client = boto3.client('dynamodb', region_name=region)
            logger.debug(f"DynamoDB client initialized for region: {region}")

            table_name = 'BillingInfo'
            current_time = datetime.datetime.now()
            formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")
            
            item = {
                'ownerId': {'S': data['owner_id']},
                'timestamp': {'S': formatted_time},
                'billing': {'S': json.dumps(data)},
            }

            logger.debug(f"Preparing to write to DynamoDB table: {table_name}")
            response = client.put_item(
                TableName=table_name,
                Item=item
            )
            logger.info(f"BillingViewSet.log() - Successfully logged billing data to DynamoDB, owner_id: {data.get('owner_id')}")
        except Exception as e:
            logger.error(f"BillingViewSet.log() - Failed to log billing data to DynamoDB: {str(e)}")


class SummaryViewSet(viewsets.ViewSet):
    def list(self, request, pk=None):
        span = trace.get_current_span()
        
        span.set_attribute("billing_summary_request", 1)

        current_minute = timezone.now().minute
        num_summaries = 50 if current_minute % 5 == 0 else 2
        
        cache_key = f'billing_summary_last_7_days_{current_minute % num_summaries}'
        summary = cache.get(cache_key)
        
        if summary is None:
            span.set_attribute("billing_summary_cache_hit", 0)
            
            time.sleep(2)
            
            billings = Billing.objects.all()
            
            summary = {
                'total_count': billings.count(),
                'total_amount': billings.aggregate(Sum('payment'))['payment__sum'] or 0,
                'period': 'all_time'
            }
            
            cache.set(cache_key, summary, 300)
        else:
            span.set_attribute("billing_summary_cache_hit", 1)
        
        return Response(summary)


class HealthViewSet(viewsets.ViewSet):
    def list(self, request):
        logger.info("HealthViewSet.list() called - Health check requested")
        logger.info("HealthViewSet.list() - Service is healthy")
        return Response({'message':'ok'}, status=status.HTTP_200_OK)
