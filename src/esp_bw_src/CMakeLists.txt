# Top-level ESP-IDF project for the BirdWatch ESP32-S3 firmware.
# Build with: idf.py set-target esp32s3 && idf.py build
cmake_minimum_required(VERSION 3.22)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(birdwatch)
