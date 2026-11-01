UniOpt-X - Constraint-Based University Timetable Generation and Optimization System

 About the Project
UniOpt-X is a web-based timetable generation and optimization system designed to simplify the process of creating university timetables. It uses predefined scheduling constraints to generate and validate timetables while helping reduce scheduling conflicts involving faculty, classrooms, and sections.

 Features
- Automated Timetable Generation: Generates timetables using defined scheduling constraints.
- Constraint Validation: Checks scheduling conflicts and helps identify invalid allocations.
- Faculty Management: Stores and manages faculty-related information.
- Room Management: Handles classroom and room allocation.
- Subject and Section Management: Organizes subjects, sections, and timetable data.
- Optimization Engine: Supports timetable optimization based on scheduling requirements.
- Web Dashboard: Provides a user-friendly interface to view timetable information.

 Technologies Used
- Backend: Python, Flask
- Database: Oracle Database
- Frontend: HTML, CSS, JavaScript
- Database Connectivity: Python oracledb
- Environment Management: python-dotenv

 📁 Project Structure
UniOpt-X/
│
├── app.py
├── optimization_engine.py
├── test_connection.py
├── requirements.txt
├── .gitignore
│
└── templates/
    ├── index.html
    └── dashboard.html


 Setup and Installation

1. Clone the Repository
git clone https://github.com/mandovrarashi-png/UniOpt-X.git
cd UniOpt-X


 2. Install Dependencies
pip install -r requirements.txt

 3. Configure Environment Variables
Create a .env file in the project root directory and add your local Oracle database configuration:

env
DB_DSN=localhost:1521/FREEPDB1
DB_USER=YOUR_DATABASE_USERNAME
DB_PASSWORD=YOUR_DATABASE_PASSWORD


 4. Run the Application
python app.py


Open the local URL displayed in your terminal to access the application.

 🎯 Project Objective
The objective of UniOpt-X is to make university timetable scheduling more organized and efficient through automated generation, constraint validation, and optimization.

 👨‍💻 Developed By
UniOpt-X Project Team
