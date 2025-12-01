# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import os
from aws_cdk import (
    Stack,
    aws_dynamodb as dynamodb_,
    aws_lambda as lambda_,
    aws_apigateway as apigw_,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_synthetics as synthetics,
    Duration,
)
from constructs import Construct

TABLE_NAME = "demo_table"


class ApigwHttpApiLambdaDynamodbPythonCdkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # VPC
        vpc = ec2.Vpc(
            self,
            "Ingress",
            cidr="10.1.0.0/16",
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Private-Subnet", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24
                )
            ],
        )
        
        # Create VPC endpoint
        dynamo_db_endpoint = ec2.GatewayVpcEndpoint(
            self,
            "DynamoDBVpce",
            service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
            vpc=vpc,
        )

        # This allows to customize the endpoint policy
        dynamo_db_endpoint.add_to_policy(
            iam.PolicyStatement(  # Restrict to listing and describing tables
                principals=[iam.AnyPrincipal()],
                actions=[                "dynamodb:DescribeStream",
                "dynamodb:DescribeTable",
                "dynamodb:Get*",
                "dynamodb:Query",
                "dynamodb:Scan",
                "dynamodb:CreateTable",
                "dynamodb:Delete*",
                "dynamodb:Update*",
                "dynamodb:PutItem"],
                resources=["*"],
            )
        )

        # Create DynamoDb Table
        demo_table = dynamodb_.Table(
            self,
            TABLE_NAME,
            partition_key=dynamodb_.Attribute(
                name="id", type=dynamodb_.AttributeType.STRING
            ),
        )

        # Create the Lambda function to receive the request with X-Ray tracing enabled
        api_hanlder = lambda_.Function(
            self,
            "ApiHandler",
            function_name="apigw_handler",
            runtime=lambda_.Runtime.PYTHON_3_9,
            code=lambda_.Code.from_asset("lambda/apigw-handler"),
            handler="index.handler",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            memory_size=1024,
            timeout=Duration.minutes(5),
            tracing=lambda_.Tracing.ACTIVE,
        )

        # grant permission to lambda to write to demo table
        demo_table.grant_write_data(api_hanlder)
        api_hanlder.add_environment("TABLE_NAME", demo_table.table_name)

        # Create API Gateway with X-Ray tracing enabled
        api = apigw_.LambdaRestApi(
            self,
            "Endpoint",
            handler=api_hanlder,
            deploy_options=apigw_.StageOptions(
                tracing_enabled=True
            )
        )

        # Create CloudWatch Synthetic Canary for proactive monitoring
        canary = synthetics.Canary(
            self,
            "ApiCanary",
            canary_name="apigw-endpoint-monitor",
            runtime=synthetics.Runtime.SYNTHETICS_NODEJS_PUPPETEER_6_2,
            test=synthetics.Test.custom(
                handler="index.handler",
                code=synthetics.Code.from_inline(f"""
const synthetics = require('Synthetics');
const log = require('SyntheticsLogger');
const https = require('https');
const http = require('http');
const {{ URL }} = require('url');

const apiCanaryBlueprint = async function () {{
    const url = '{api.url}';
    const parsedUrl = new URL(url);
    
    const requestOptions = {{
        hostname: parsedUrl.hostname,
        path: parsedUrl.pathname,
        method: 'GET',
        port: parsedUrl.port || (parsedUrl.protocol === 'https:' ? 443 : 80)
    }};
    
    const protocol = parsedUrl.protocol === 'https:' ? https : http;
    
    return new Promise((resolve, reject) => {{
        const req = protocol.request(requestOptions, (res) => {{
            log.info(`Status Code: ${{res.statusCode}}`);
            if (res.statusCode === 200 || res.statusCode === 403) {{
                resolve();
            }} else {{
                reject(new Error(`Failed with status code: ${{res.statusCode}}`));
            }}
        }});
        
        req.on('error', (error) => {{
            reject(error);
        }});
        
        req.end();
    }});
}};

exports.handler = async () => {{
    return await apiCanaryBlueprint();
}};
""")
            ),
            schedule=synthetics.Schedule.rate(Duration.minutes(5))
        )
