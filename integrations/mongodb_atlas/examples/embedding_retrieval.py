# Install required packages for this example, including mongodb-atlas-haystack and other libraries needed
# for Markdown conversion and embeddings generation. Use the following command:
#
# pip install mongodb-atlas-haystack markdown-it-py mdit_plain "sentence-transformers>=2.2.0"
#
# Download some Markdown files to index.
# git clone https://github.com/anakin87/neural-search-pills

import glob

from haystack import Pipeline
from haystack.components.converters import MarkdownToDocument
from haystack.components.embedders import SentenceTransformersDocumentEmbedder, SentenceTransformersTextEmbedder
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.writers import DocumentWriter

from haystack_integrations.components.retrievers.mongodb_atlas import MongoDBAtlasEmbeddingRetriever
from haystack_integrations.document_stores.mongodb_atlas import MongoDBAtlasDocumentStore

# To use the MongoDBAtlasDocumentStore, you must have a running MongoDB Atlas database.
# For details, see https://www.mongodb.com/docs/atlas/getting-started/
# NOTE: you need to create manually the vector search index and the full text search
#       index in your MongoDB Atlas database.

# Once your database is set, set the environment variable `MONGO_CONNECTION_STRING`
# with the connection string to your MongoDB Atlas database.
# format: "mongodb+srv://{mongo_atlas_username}:{mongo_atlas_password}@{mongo_atlas_host}/?{mongo_atlas_params_string}".

# Initialize the document store
document_store = MongoDBAtlasDocumentStore(
    database_name="haystack_test",
    collection_name="test_collection",
    vector_search_index="test_vector_search_index",
    full_text_search_index="test_full_text_search_index",
)

# This is to avoid duplicates in the collection
print(f"Cleaning up collection {document_store.collection_name}")
document_store.collection.delete_many({})

# Create the indexing Pipeline and index some documents
file_paths = glob.glob("neural-search-pills/pills/*.md")

print("Creating indexing pipeline")
indexing = Pipeline()
indexing.add_component("converter", MarkdownToDocument())
indexing.add_component("splitter", DocumentSplitter(split_by="sentence", split_length=2))
indexing.add_component("embedder", SentenceTransformersDocumentEmbedder())
indexing.add_component("writer", DocumentWriter(document_store))
indexing.connect("converter", "splitter")
indexing.connect("splitter", "embedder")
indexing.connect("embedder", "writer")

print(f"Running indexing pipeline with {len(file_paths)} files")
indexing.run({"converter": {"sources": file_paths}})

print("Creating querying pipeline")
querying = Pipeline()
querying.add_component("embedder", SentenceTransformersTextEmbedder())
querying.add_component("retriever", MongoDBAtlasEmbeddingRetriever(document_store=document_store, top_k=3))
querying.connect("embedder", "retriever")

query = "What is a cross-encoder?"
print(f"Running querying pipeline with query: '{query}'")
results = querying.run({"embedder": {"text": "What is a cross-encoder?"}})

print(f"Results: {results}")
for doc in results["retriever"]["documents"]:
    print(doc)
    print("-" * 10)
