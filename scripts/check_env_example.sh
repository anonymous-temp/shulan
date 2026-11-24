#!/bin/bash

check-env --example .env.example --env .env.example.dev
check-env --example .env.example --env .env.example.test
check-env --example .env.example --env .env.example.production
