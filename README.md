# FIFO Management for Comestible Storage Units

## Design and Implementation of a Smart Interface for Automating FIFO Stock Control in Comestible Storage Units

---

## Overview

This project is a Raspberry Pi based smart inventory management system designed for commercial kitchens, hotels, restaurants, canteens, and food storage facilities. The system automates the **First-In First-Out (FIFO)** inventory principle by tracking food items through QR codes, monitoring expiry times, and providing visual and audible alerts.

The system uses:

* Raspberry Pi 5
* HDMI Main Display
* 3.5-inch SPI Secondary Display
* USB Camera
* Thermal QR Printer
* Rotary Encoders
* Inductive Proximity Sensor
* Buzzer
* LED Indicators

The objective is to minimize food wastage, improve stock traceability, and simplify inventory management operations.

---

## Features

### FIFO Inventory Management

* Automatic FIFO tracking.
* Every food item receives a unique QR code.
* Oldest stock can be identified and removed first.

### QR Code Based Tracking

* Generates unique QR IDs.
* Prints QR labels using thermal printer.
* QR scanning updates inventory automatically.

### Expiry Monitoring

* Tracks shelf life of each food item.
* Detects expired inventory automatically.
* Generates alarm notifications.

### Dual Display Architecture

#### HDMI Display (Main Interface)

* Live inventory table
* Camera feed
* Item status monitoring
* Printer status
* Sensor status
* Alarm status

#### 3.5" SPI Display (Secondary Interface)

* Encoder-based setup screen
* Weight adjustment interface
* Camera preview
* Status notifications

### Weight Management

* Rotary encoder based weight selection
* Weight update after scanning QR codes
* Real-time synchronization between displays

### Alarm Management

* Buzzer alerts for expired items
* Temporary silence feature
* Automatic re-alert if expired item remains
* Permanent alarm cancellation when expired item is removed

### Shared State Synchronization

* Thread-safe state management
* Atomic JSON communication
* Inter-process communication between displays

---

# System Architecture

```
                      ┌────────────────────┐
                      │     USB Camera     │
                      └─────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │   HDMI Display App  │
                     │   hdmi_display.py   │
                     └─────────┬───────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                    │
          ▼                    ▼                    ▼

   QR Printer         Shared State          Proximity Sensor
                         JSON File

          ▲                    ▲
          │                    │
          │                    │
          ▼                    ▼

                  Small Display App
                   small_display.py

          ▲                    ▲
          │                    │
          ▼                    ▼

      Encoder 1            Encoder 2
      (Weight)             (Item Select)
```

---

# Project Structure

```
FIFO-System/
│
├── hdmi_display.py
├── small_display.py
├── shared_state.py
│
├── README.md
│
└── assets/
    ├── images/
    └── diagrams/
```

---

# Core Modules

## 1. shared_state.py

Acts as the central communication layer between both display applications.

### Responsibilities

* Stores inventory database
* Maintains display states
* Tracks encoder values
* Handles buzzer status
* Manages QR counters
* Synchronizes data across processes

### IPC Files

```
/tmp/fifo_state.json
```

Stores system state.

```
/dev/shm/fifo_frame.npy
```

Stores live camera frames in RAM.

---

## 2. hdmi_display.py

Main application running on the HDMI display.

### Functions

* Inventory dashboard
* QR code scanning
* Camera processing
* Expiry monitoring
* Thermal printer control
* Proximity sensor handling
* Buzzer control

### Display Information

* Inventory table
* QR IDs
* Item names
* Weight
* Shelf life
* Time remaining
* Expiry status

---

## 3. small_display.py

Secondary display application.

### Functions

* Encoder interface
* Weight adjustment
* Item selection
* Camera preview
* Status messages

### Supported Modes

| Mode         | Function               |
| ------------ | ---------------------- |
| startup      | System startup         |
| idle         | Waiting state          |
| setup        | Add item               |
| update       | Update weight          |
| camera       | Live camera            |
| printing     | QR printing            |
| fresh_scan   | Fresh item removed     |
| expired_scan | Expired item discarded |
| timeout      | Expiry alert           |

---

# Hardware Components

| Component            | Purpose             |
| -------------------- | ------------------- |
| Raspberry Pi 5       | Main Controller     |
| HDMI Display         | Main Dashboard      |
| 3.5" SPI TFT Display | Secondary Interface |
| USB Camera           | QR Scanning         |
| Thermal Printer      | QR Label Printing   |
| Rotary Encoder 1     | Weight Selection    |
| Rotary Encoder 2     | Item Selection      |
| Inductive Sensor     | Item Detection      |
| Buzzer               | Expiry Alert        |
| LED                  | Sensor Status       |

---

# GPIO Configuration

## HDMI Controller

| Device | GPIO    |
| ------ | ------- |
| Sensor | GPIO 16 |
| Buzzer | GPIO 12 |
| LED    | GPIO 13 |

## Small Display

### Encoder 1

| Signal | GPIO    |
| ------ | ------- |
| CLK    | GPIO 19 |
| DT     | GPIO 20 |
| SW     | GPIO 5  |

### Encoder 2

| Signal | GPIO    |
| ------ | ------- |
| CLK    | GPIO 21 |
| DT     | GPIO 26 |
| SW     | GPIO 6  |

---

# Inventory Workflow

## Adding a New Item

1. Press SPACE.
2. System enters setup mode.
3. Encoder 2 selects food item.
4. Encoder 1 selects weight.
5. Press SPACE again.
6. QR label is printed.
7. Item is stored in inventory database.

---

## Updating an Existing Item

1. Place item near scanner.
2. QR code is detected.
3. System enters update mode.
4. Encoder 1 adjusts weight.
5. Press Encoder 1 switch.
6. Database updates weight.

---

## Expiry Detection

1. Expiry thread continuously checks inventory.
2. Expired items are flagged.
3. Buzzer activates.
4. Alert displayed on screens.
5. Scanning expired item removes it from database.

---

# Software Requirements

## Operating System

* Raspberry Pi OS Bookworm

## Python Version

```
Python 3.11+
```

## Required Libraries

```bash
pip install numpy
pip install pillow
pip install opencv-python
pip install pyzbar
pip install python-escpos
pip install rpi-lgpio
```

---

# Installation

Clone repository:

```bash
git clone https://github.com/yourusername/FIFO-Comestible-Storage.git

cd FIFO-Comestible-Storage
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Run HDMI application:

```bash
python3 hdmi_display.py
```

Run Small Display:

```bash
sudo python3 small_display.py
```

---

# Communication Mechanism

The system uses a lightweight Inter-Process Communication (IPC) mechanism.

### State Communication

```
/tmp/fifo_state.json
```

Stores:

* Inventory database
* Display mode
* Encoder values
* Alarm state
* Active QR ID

### Camera Communication

```
/dev/shm/fifo_frame.npy
```

Stores live camera frames in RAM.

Benefits:

* Extremely fast
* No SD card wear
* Atomic state updates
* Reliable synchronization

---

# Safety Features

* Thread-safe shared state
* Atomic file writes
* Automatic buzzer reset
* Printer failure handling
* Camera failure recovery
* GPIO cleanup on shutdown
* QR scan cooldown protection

---

# Future Improvements

* Cloud inventory synchronization
* Mobile application integration
* RFID support
* AI-based food spoilage prediction
* Temperature and humidity monitoring
* Multi-storage unit networking
* Database backend (SQLite/PostgreSQL)
* Web dashboard

---

# Applications

* Hotels
* Restaurants
* Industrial Kitchens
* Catering Services
* Food Warehouses
* Hospitals
* Hostels
* Supermarkets

---

# Authors

Developed as part of a smart automation project for:

**FIFO Management of Comestible Storage Units**

Focused on reducing food wastage, improving traceability, and ensuring efficient stock rotation through automation.

---

# License

This project is released under the MIT License.

Feel free to use, modify, and distribute the software for educational and research purposes.
