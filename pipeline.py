from pymongo import MongoClient
from langchain_openai import ChatOpenAI
from langchain_core.messages import (
    HumanMessage,
    trim_messages,
)
from langchain.storage import InMemoryByteStore
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnablePassthrough
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_community.document_loaders import TextLoader
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import Chroma
from langchain.retrievers.multi_vector import MultiVectorRetriever
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from langchain.retrievers.self_query.base import SelfQueryRetriever
from langchain.retrievers.document_compressors import LLMChainFilter
from langchain.retrievers import ContextualCompressionRetriever
from operator import itemgetter
import uuid
import os
from util import *
import openai

class AIAssistant:
    def __init__(self):
        self.model = ChatOpenAI(model="gpt-4o-mini-2024-07-18", temperature=0)
        self.embedding = OpenAIEmbeddings(model="text-embedding-3-large")
        self.parser = StrOutputParser()
        self.retriever = self.selfquerying_retriever()
        
    def llm_memory(self):
        trimmer = trim_messages(
            max_tokens=500,
            strategy="last",
            token_counter=self.model,
            include_system=True,
            allow_partial=False,
            start_on="human",
        )
        query = {'username': 'phuccngo'}
        dbclient = MongoClient('mongodb://localhost:27017/')
        db = dbclient['company_statement_chat_database']
        conversations_collection = db['messages']
        result = conversations_collection.find_one(query)
        return trimmer
    
    def prompt_gen(self):
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    """
                    Bạn là AI Chatbot của Công ty CP TM & SX Bao Bì Ánh Sáng hay BBAS. 
                    Bạn ở đây để trả lời tất cả các câu hỏi về văn hóa doanh nghiệp với các tài liệu bạn được cung cấp. 
                    Tài liệu được cung cấp: {doc} 
                    Nếu tài liệu được cung cấp là "Không có tài liệu liên quan đến câu hỏi hoặc yêu cầu này." hoặc các tài liệu được cung cấp không liên quan đến câu hỏi hoặc yêu cầu thì trả lời là \"Tôi không được cung cấp thông tin để trả lời câu hỏi này\"
                    Lưu ý: CÁC CHỈ TRẢ LỜI CÁC CÂU HỎI HOẶC YÊU CẦU ĐƯỢC HỎI. CÁC ĐỊNH NGHĨA CŨNG NHƯ CÁC THÔNG ĐIỆP NÊN GIỮ NGUYÊN, KHÔNG THAY ĐỔI. KHÔNG RÚT GỌN TÀI LIỆU ĐỂ TRẢ LỜI.
                    Câu hỏi:
                    """,
                ),
                MessagesPlaceholder(variable_name="question"), #phần "question" bên dưới sẽ được chèn vào đây
            ]
        )
        return prompt
    
    def selfquerying_retriever(self):
        docs, metadata_field_info = bbas_docs()
        vectorstore = Chroma.from_documents(docs, self.embedding)
        document_content_description = "Content of each topic"
        retriever = SelfQueryRetriever.from_llm(
            self.model,
            vectorstore,
            document_content_description,
            metadata_field_info,
            enable_limit=True,
        )
        return retriever
    
    def multivector_retriever(self):
        loaders = [
            TextLoader("BBAS.txt", encoding = 'UTF-8'),
        ]
        docs = []
        for loader in loaders:
            docs.extend(loader.load())
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000)
        docs = text_splitter.split_documents(docs)
        vectorstore = Chroma(
            collection_name="full_documents", embedding_function=self.embedding
        )
        store = InMemoryByteStore()
        id_key = "doc_id"
        retriever = MultiVectorRetriever(
            vectorstore=vectorstore,
            byte_store=store,
            id_key=id_key,
        )
        doc_ids = [str(uuid.uuid4()) for _ in docs]
        child_text_splitter = RecursiveCharacterTextSplitter(chunk_size=200)
        sub_docs = []
        for i, doc in enumerate(docs):
            _id = doc_ids[i]
            _sub_docs = child_text_splitter.split_documents([doc])
            for _doc in _sub_docs:
                _doc.metadata[id_key] = _id
            sub_docs.extend(_sub_docs)
        retriever.vectorstore.add_documents(sub_docs)
        retriever.docstore.mset(list(zip(doc_ids, docs)))
        return retriever
        
    
    def docs_gen(self, question):
        en_question = vn_2_en(question)
        try:
            _filter = LLMChainFilter.from_llm(self.model)
            compression_retriever = ContextualCompressionRetriever(
                base_compressor=_filter, base_retriever=self.retriever
            )
            docs = compression_retriever.invoke(en_question)
        except Exception as e:
            docs = [Document(page_content="Không có tài liệu liên quan đến câu hỏi này.", metadata={})]
        return "\n\n".join(doc.page_content for doc in docs)

    def invoke(self, question: str, session_id: str) -> str:
        trimmer = self.llm_memory()
        prompt = self.prompt_gen()
        chain = (
            RunnablePassthrough.assign(messages=itemgetter("question") | trimmer)
            | (lambda output: (output.update({'question': output['messages']}), output)[-1])
            | prompt
            | self.model
            | self.parser
        )
        with_message_history = RunnableWithMessageHistory(
            chain,
            get_session_history_mongodb,
            input_messages_key="question", #Which key is user's input
        )
        docs = self.docs_gen(question)
        print(docs)
        print("/////////////////////////////////////////////")
        config = {"configurable": {"session_id": session_id}}
        result = with_message_history.invoke(
            {
                "question": [HumanMessage(content=question)],
                "doc": docs
            },
            config=config,
        )
        return result
    
    def stream(self, question: str, session_id: str) -> str:
        config = {"configurable": {"session_id": session_id}}
        trimmer = self.llm_memory()
        prompt = self.prompt_gen()
        chain = (
            RunnablePassthrough.assign(messages=itemgetter("question") | trimmer)
            | (lambda output: (output.update({'question': output['messages']}), output)[-1])
            | prompt
            | self.model
            | self.parser
        )
        with_message_history = RunnableWithMessageHistory(
            chain,
            get_session_history_mongodb,
            input_messages_key="question", #Which key is user's input
        )
        docs = self.docs_gen(question)
        print(docs)
        print("/////////////////////////////////////////////")
        result = with_message_history.invoke(
            {
                "question": [HumanMessage(content=question)],
                "doc": docs
            },
            config=config,
        )
        dict_input = {
            "question": [HumanMessage(content=question)],
            "doc": docs
        }
        return with_message_history, dict_input, config