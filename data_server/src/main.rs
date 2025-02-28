use clap::Parser;
use log::info;
use prost::Message;
use rand::prelude::IteratorRandom;
use rand::seq::SliceRandom;
use rand::thread_rng;
use std::fs::File;
use std::io::{self, BufReader, Read, Result as IoResult};
use std::vec;
use tonic::{transport::Server, Request, Response, Status};

pub mod text_data {
    tonic::include_proto!("text_data");
}

use text_data::{
    data_service_server::{DataService, DataServiceServer},
    SampleDataRequest, SampledData, Sentence, TextData,
};

#[derive(Default)]
pub struct MyDataService {
    groups: Vec<TextData>,
    weights: Vec<f32>,
}

fn read_pb_stream<R: Read>(mut reader: BufReader<R>) -> io::Result<Vec<TextData>> {
    let mut text_data_list = Vec::new();
    let mut index = 0;

    loop {
        let mut size_buf = [0u8; 4];
        match reader.read_exact(&mut size_buf) {
            Ok(()) => (),
            Err(ref e) if e.kind() == io::ErrorKind::UnexpectedEof => break, // End of file
            Err(e) => return Err(e),
        }

        let size = u32::from_le_bytes(size_buf) as usize;

        let mut message_buf = vec![0u8; size];
        reader.read_exact(&mut message_buf)?;

        let text_data = TextData::decode(&message_buf[..])
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        text_data_list.push(text_data);

        index += 1;

        if index % 10000 == 0 {
            info!("Loaded {} groups", index);
        }
    }

    Ok(text_data_list)
}

impl MyDataService {
    pub fn new(files: Vec<String>) -> IoResult<Self> {
        let mut groups = Vec::new();
        let mut weights = Vec::new();

        for filename in files.iter() {
            let file = File::open(filename)?;
            let reader = BufReader::new(file);

            // Assuming read_pb_stream is implemented and it returns an iterator over TextData
            for text_data in read_pb_stream(reader)? {
                groups.push(text_data.clone());
                weights.push(text_data.sentences.len() as f32); // Assuming sentences is a repeated field in TextData
            }
        }

        info!("Loaded {} groups", groups.len());

        Ok(MyDataService { groups, weights })
    }
}

#[tonic::async_trait]
impl DataService for MyDataService {
    async fn sample_data(
        &self,
        request: Request<SampleDataRequest>,
    ) -> Result<Response<SampledData>, Status> {
        let mut num_samples = request.into_inner().num_samples as usize;
        let mut rng = thread_rng();

        let group = self
            .groups
            .choose_weighted(&mut rng, |item| item.sentences.len() as f32);

        if group.is_ok() {
            let group = group.unwrap();
            if num_samples > group.sentences.len() {
                num_samples = group.sentences.len();
            }

            let sentences_ref = group
                .sentences
                .iter()
                .choose_multiple(&mut rng, num_samples);

            let sentences: Vec<Sentence> = sentences_ref
                .into_iter()
                .cloned() // Clone each &Sentence to get Sentence
                .collect();

            Ok(Response::new(SampledData { samples: sentences }))
        } else {
            Err(Status::internal("Failed to select a group"))
        }
    }
}

/// My Data Service Application
#[derive(Parser, Debug)]
#[clap(author, version, about, long_about = None)]
struct Args {
    /// Files to process
    #[clap(short, long, value_name = "FILE", required = true)]
    files: Vec<String>,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    env_logger::init();

    // Parse command-line arguments
    let args = Args::parse();

    let addr = "[::1]:50051".parse()?;
    let data_service = MyDataService::new(args.files)?;

    info!("Starting server at {}", addr);

    Server::builder()
        .add_service(DataServiceServer::new(data_service))
        .serve(addr)
        .await?;

    Ok(())
}
