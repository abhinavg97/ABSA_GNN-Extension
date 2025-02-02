import dgl
import spacy
import torch
import numpy as np

from ..utils import utils
from ..utils import graph_utils

from logger.logger import logger
from config import configuration as cfg


class DGL_Graph(object):
    """
    Creates a DGL graph with training and testing functionality
    """
    def __init__(self, dataset_df, nlp=spacy.load('en_core_web_lg')):
        # counter is the variable storing the total number of docs + tokens
        self.total_nodes = 0
        self.id_to_vector = {}
        self.word_to_id = {}
        self.nlp = nlp
        words = {}
        counter = 0
        self.docs = [[] for i in range(dataset_df.shape[0])]
        for index, text in enumerate(dataset_df['text'].tolist()):
            tokens = self.nlp(text)
            for token in tokens:
                try:
                    words[token.text]
                    self.docs[index] += [self.word_to_id[token.text]]
                except KeyError:
                    words[token.text] = 1
                    self.id_to_vector[counter] = token.vector
                    self.word_to_id[token.text] = counter
                    self.docs[index] += [self.word_to_id[token.text]]
                    counter += 1
        self.total_nodes = counter + dataset_df.shape[0]
        self.dataframe = dataset_df
        logger.info("Processed {} tokens.".format(len(self.word_to_id)))

    def visualize_dgl_graph(self, graph):
        """
        visualize single dgl graph
        Args:
            graph: dgl graph
        """
        graph_utils.visualize_dgl_graph_as_networkx(graph)

    def save_graphs(self, path, graphs, labels_dict=None):

        if labels_dict is not None:
            labels_dict_tensor = {"glabel": torch.tensor(labels_dict["glabel"])}
        else:
            labels_dict_tensor = None

        graph_utils.save_dgl_graphs(path, graphs, labels_dict_tensor)
        logger.info(f"Saving {path}")

    def create_instance_dgl_graphs(self):
        """
        Constructs individual DGL graphs for each of the data instance
        Returns:
            graphs: An array containing DGL graphs
        """
        graphs = []
        labels = []
        for _, item in self.dataframe.iterrows():
            graphs += [self.create_instance_dgl_graph(item['text'])]
            labels += [item['labels']]
        labels_dict = {"glabel": labels}
        return graphs, labels_dict

    def create_instance_dgl_graph(self, text):
        """
        Create a single DGL graph
        NOTE: DGL only supports sequential node ids
        Args:
            text: Input data in string format

        Returns:
            DGL Graph: DGL graph for the input text
        """
        tokens = self.nlp(text)
        node_embeddings = []         # node embedding
        edges_sources = []          # edge data
        edges_dest = []             # edge data
        node_counter = 0            # uniq ids for the tokens in the document
        uniq_token_ids = {}         # ids to map token to id for the dgl graph
        token_ids = []              # global unique ids for the tokens

        for token in tokens:
            try:
                uniq_token_ids[token.text]
            except KeyError:
                uniq_token_ids[token.text] = node_counter
                node_embeddings.append(token.vector)
                node_counter += 1
                token_ids += [self.word_to_id[token.text]]

        for token in tokens:
            for child in token.children:
                edges_sources.append(uniq_token_ids[token.text])
                edges_dest.append(uniq_token_ids[child.text])

        # add edges and node embeddings to the graph
        g = dgl.graph(data=(edges_sources, edges_dest), num_nodes=len(uniq_token_ids))
        g = dgl.add_self_loop(g)
        g.ndata['emb'] = torch.tensor(node_embeddings).float()
        # add token id attribute to node
        g.ndata['item_id'] = torch.tensor(token_ids).long()
        return g

    def _compute_doc_embedding(self, node_id):
        """
        computes doc embedding by taking average of all word vectors in a document
        Args:
            node_id: id of the node in the graph

        Returns:
            embedding: averaged vector of all words vectors in the doc
        """
        doc_id = node_id - len(self.word_to_id)
        embedding = np.zeros(len(self.id_to_vector[0]))

        for word_id in self.docs[doc_id]:
            embedding += np.array(self.id_to_vector[word_id])

        embedding = embedding / len(self.docs[doc_id])
        return embedding

    def create_large_dgl_graph(self):
        """
        Creates a complete dgl graph tokens and documents as nodes
        """
        g = dgl.DGLGraph()
        g.add_nodes(self.total_nodes)

        # compute ids and embeddings for vocab nodes
        ids = []
        embedding = []
        for id, __ in enumerate(self.word_to_id):
            ids += [id]
            embedding += [np.array(self.id_to_vector[id])]

        # compute ids and embeddings for doc nodes
        # at least one word is expected in the corpus
        for id in range(len(self.word_to_id), self.total_nodes):
            ids += [id]
            embedding += [self._compute_doc_embedding(id)]

        # add items ids and embeddings for vocan nodes and doc nodes
        g.ndata['item_id'] = torch.tensor(ids)
        g.ndata['emb'] = torch.tensor(embedding)

        # add edges and edge data betweem vocab words in the dgl graph
        pmi = utils.pmi(self.dataframe)
        edges_sources = []
        edges_dest = []
        edge_data = []
        for tuples in pmi:
            word_pair = tuples[0]
            pmi_score = tuples[1]
            word1 = word_pair[0]
            word2 = word_pair[1]
            word1_id = self.word_to_id[word1]
            word2_id = self.word_to_id[word2]
            edges_sources += [word1_id]
            edges_dest += [word2_id]
            edge_data += [[pmi_score]]
        g.add_edges(torch.tensor(edges_sources), torch.tensor(edges_dest),
                    {'weight': torch.tensor(edge_data)})

        # add edges and edge data between documents
        if cfg['data']['multi_label']:
            labels = self.dataframe['labels'].tolist()
            edges_sources = []
            edges_dest = []
            edge_data = []
            for i1 in range(len(labels)):
                for i2 in range(i1 + 1, len(labels)):
                    doc1_id = len(self.word_to_id) + i1
                    doc2_id = len(self.word_to_id) + i2
                    weight = utils.iou(list(labels[i1]), list(labels[i2]))
                    edges_sources += [doc1_id, doc2_id]
                    edges_dest += [doc2_id, doc1_id]
                    edge_data += [[weight], [weight]]
            g.add_edges(torch.tensor(edges_sources), torch.tensor(edges_dest),
                        {'weight': torch.tensor(edge_data)})

        # add edges and edge data between word and documents
        tf_idf_df = utils.tf_idf(self.dataframe, vocab=self.word_to_id)
        edges_sources = []
        edges_dest = []
        edge_data = []
        for index, doc_row in tf_idf_df.iterrows():
            doc_id = len(self.word_to_id) + index
            for word, tf_idf_value in doc_row.items():
                word_id = self.word_to_id[word]
                edges_sources += [doc_id]
                edges_dest += [word_id]
                edge_data += [[tf_idf_value]]
        g.add_edges(torch.tensor(edges_sources), torch.tensor(edges_dest),
                    {'weight': torch.tensor(edge_data)})

        g = dgl.add_self_loop(g)

        return g
