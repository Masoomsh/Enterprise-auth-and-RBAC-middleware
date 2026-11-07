import os
import time
import truststore
 
truststore.inject_into_ssl()
 
from dotenv import load_dotenv
from pinecone import Pinecone, ServerlessSpec
 
 
load_dotenv()
 
 
class PineconeClient:
 
    def __init__(self):
 
        api_key = os.getenv("PINECONE_API_KEY")
 
        if not api_key:
            raise ValueError("PINECONE_API_KEY not found in .env")
 
        self.pc = Pinecone(api_key=api_key)
 
        index_name = "semantic-cache"
 
        existing_indexes = [
            index.name for index in self.pc.list_indexes()
        ]
 
        if index_name not in existing_indexes:
 
            self.pc.create_index(
                name=index_name,
                dimension=768,
                metric="cosine",
                spec=ServerlessSpec(
                    cloud="aws",
                    region="us-east-1"
                )
            )
 
            print("Creating Pinecone index...")
 
            while not self.pc.describe_index(index_name).status["ready"]:
                time.sleep(2)
 
        self.index = self.pc.Index(index_name)