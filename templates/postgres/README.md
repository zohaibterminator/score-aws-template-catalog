# PostgreSQL

Creates a private PostgreSQL RDS instance, database security group, and DB subnet group. Score supplies the password. Production enables Multi-AZ, backups, final snapshot, and deletion protection. To destroy production RDS, explicitly disable deletion protection in a reviewed change before deleting the CR.
